#!/usr/bin/env python

import glob
import os
import re
import shutil
import sys
import tarfile
import time
from enum import Enum
from zipfile import ZipFile
from watson_machine_learning_client import WatsonMachineLearningAPIClient

from utils.debug import debug
from utils.os_util import copy_dir
from utils.config import YAMLReader, ConfigParseError, ConfigurationError
from utils.wml import WMLWrapper
from utils.cos import COSWrapper, COSWrapperError, BucketNotFoundError


class ExitCode(Enum):
    """
    Defines the exit codes for this utility
    """
    SUCCESS = 0
    INCORRECT_INVOCATION = 1
    ENV_ERROR = 2
    CONFIGURATION_ERROR = 3
    PRE_PROCESSING_FAILED = 4
    TRAINING_FAILED = 5
    DOWNLOAD_FAILED = 6
    EXTRACTION_FAILED = 7
    COPY_FAILED = 8


TRAINING_LOG_NAME = 'training-log.txt'  # fixed; do not change
TRAINING_OUTPUT_ARCHIVE_NAME = 'model_training_output.tar.gz'  # do not change


def print_banner(message):
    print('# --------------------------------------------------------')
    print('# {}'.format(message))
    print('# --------------------------------------------------------')


# --------------------------------------------------------
# Process command line parameters
# --------------------------------------------------------


def process_cmd_parameters():
    """
        Process command line parameters. This function terminates the
         application if an invocation error was detected.
        :returns: dict, containing two properties: 'config_file' and
         'command'
        :rtype: dict
    """

    def display_usage():
        print('Train a MAX model using Watson Machine Learning. ')
        print('\nUsage: {} <training_config_file> [command] \n'
              .format(sys.argv[0]))
        print('Valid commands:')
        print('     clean          removes local model training artifacts')
        print('     prepare        generates model training artifacts'
              ' but skips model training')
        print('     train          generates model training artifacts and'
              ' trains the model')
        print('     package        generates model training artifacts, trains'
              ' the model, and performs post processing')
        print('\n If no command is specified, "package" is used. ')

    if len(sys.argv) == 1:
        display_usage()
        sys.exit(ExitCode.SUCCESS.value)

    if os.path.isfile(sys.argv[1]) is False:
        print('Invocation error. "{}" is not a file.'.format(sys.argv[1]))
        display_usage()
        sys.exit(ExitCode.INCORRECT_INVOCATION.value)

    cmd_parameters = {
        'config_file': sys.argv[1],
        'command': 'package'
    }

    if len(sys.argv) == 2:
        cmd_parameters['config_file'] = sys.argv[1]
    else:
        if sys.argv[2].lower() in ['clean',
                                   'prepare',
                                   'train',
                                   'package']:
            cmd_parameters['command'] = sys.argv[2].lower()
        else:
            print('Invocation error. "{}" is not a valid command.'
                  .format(sys.argv[2]))
            display_usage()
            sys.exit(ExitCode.INCORRECT_INVOCATION.value)

    return cmd_parameters


cmd_parameters = process_cmd_parameters()

# --------------------------------------------------------
# Verify that the required environment variables are set
# --------------------------------------------------------


def verify_env_settings():

    print_banner('Checking environment variables ...')
    var_missing = False
    # WML environment variables
    for var_name in ['ML_ENV', 'ML_USERNAME', 'ML_PASSWORD', 'ML_INSTANCE']:
        if os.environ.get(var_name) is None:
            print(' Error. Environment variable {} is not defined.'
                  .format(var_name))
            var_missing = True

    # Cloud Object Storage environment variables
    for var_name in ['AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY']:
        if os.environ.get(var_name) is None:
            print(' Error. Environment variable {} is not defined.'
                  .format(var_name))
            var_missing = True

    if var_missing:
        sys.exit(ExitCode.ENV_ERROR.value)


verify_env_settings()

# --------------------------------------------------------
# Process configuration file
# --------------------------------------------------------

print_banner('Validating configuration file "{}" ...'
             .format(cmd_parameters['config_file']))

config = None

try:
    r = YAMLReader(cmd_parameters['config_file'])
    config = r.read()
except ConfigurationError as ce:
    for missing_setting in ce.get_missing_settings():
        print('Error. Configuration file "{}" does not'
              ' define setting "{}".'
              .format(cmd_parameters['config_file'],
                      missing_setting.get('yaml_path')))
    sys.exit(ExitCode.CONFIGURATION_ERROR.value)
except ConfigParseError as cpe:
    print('Error. Configuration file "{}" is invalid: {}'
          .format(cmd_parameters['config_file'],
                  str(cpe)))
    sys.exit(ExitCode.CONFIGURATION_ERROR.value)
except FileNotFoundError:
    print('Error. Configuration file "{}" was not found.'
          .format(cmd_parameters['config_file']))
    sys.exit(ExitCode.INVOCATION_ERROR.value)

debug('Using the following configuration settings: ', config)

# --------------------------------------------------------
# Remove existing model training artifacts
# --------------------------------------------------------

print_banner('Removing temporary work files ...')

for file in [config['model_code_archive']]:
    if os.path.isfile(file):
        os.remove(file)

# terminate if the "clean" command was specified
# when the utility was invoked
if cmd_parameters['command'] == 'clean':
    print('Skipping model training.')
    sys.exit(ExitCode.SUCCESS.value)

# --------------------------------------------------------
# Verify the Cloud Object Storage configuration:
#  - the results bucket must exist
# --------------------------------------------------------

cw = None  # handle for the Cloud Object Storage wrapper

print_banner('Verifying Cloud Object Storage setup ...')

try:
    # instantiate the Cloud Object Storage wrapper
    cw = COSWrapper(os.environ['AWS_ACCESS_KEY_ID'],
                    os.environ['AWS_SECRET_ACCESS_KEY'])

    print(' Verifying that training results bucket "{}" exists. '
          ' It will be created if necessary ...'
          .format(config['results_bucket']))

    # make sure the training results bucket exists;
    # it can be empty, but doesn't have to be
    cw.create_bucket(config['results_bucket'],
                     exist_ok=True)

    print(' Verifying that training data bucket "{}" exists. '
          ' It will be created if necessary ...'
          .format(config['training_bucket']))

    # make sure the training data bucket exists;
    cw.create_bucket(config['training_bucket'],
                     exist_ok=True)

    print(' Verifying that there is data in the training data bucket "{}". '
          .format(config['training_bucket']))

    # determine whether there is data in the training data bucket
    if cw.is_bucket_empty(config['training_bucket']):
        # the bucket is empty
        print(' No data was found in training data bucket "{}". '
              .format(config['training_bucket']))
        if config.get('local_data_dir', None) is None:
            # error. there is no local training data either;
            # abort processing
            print('Error. No local training data was found. '
                  'Please check your configuration settings.')
            sys.exit(ExitCode.PRE_PROCESSING_FAILED.value)
        # verify that local_data_dir is a directory
        if not os.path.isdir(config['local_data_dir']):
            print('Error. "{}" is not a directory or cannot be accessed.'
                  .format(config['local_data_dir']))
            sys.exit(ExitCode.PRE_PROCESSING_FAILED.value)

        # verify that there is data in the local data dir
        file_count = 0
        ignore_list = []
        ignore_list.append(os.path.join(config['local_data_dir'], 'README.md'))
        for file in glob.iglob(config['local_data_dir'] + '**/*',
                               recursive=True):
            if file in ignore_list:
                continue
            if os.path.isfile(file):
                print(' Uploading "{}" to training data bucket "{}" ...'
                      .format(file, config['training_bucket']))
                cw.upload_file(file,
                               config['training_bucket'],
                               config.get('training_data_key_prefix'),
                               file[len(config['local_data_dir']):]
                               .lstrip('/'))
                file_count += 1

        if file_count == 0:
            print('Error. No local training data was found in "{}".'
                  .format(config['local_data_dir']))
            sys.exit(ExitCode.PRE_PROCESSING_FAILED.value)
        else:
            print('Uploaded {} files to training data bucket "{}".'
                  .format(file_count, config['training_bucket']))

except ValueError as ve:
    print('Error. {}'.format(ve))
    sys.exit(ExitCode.PRE_PROCESSING_FAILED.value)
except BucketNotFoundError as bnfe:
    print('Error. {}'.format(bnfe))
    sys.exit(ExitCode.PRE_PROCESSING_FAILED.value)
except FileNotFoundError as fnfe:
    print('Error. {}'.format(fnfe))
    sys.exit(ExitCode.PRE_PROCESSING_FAILED.value)
except COSWrapperError as cwe:
    print('Error. Cloud Object Storage preparation failed: {}'.format(cwe))
    sys.exit(ExitCode.PRE_PROCESSING_FAILED.value)

# --------------------------------------------------------
# Create model building ZIP
# --------------------------------------------------------

print_banner('Locating model building files ...')

#
# 1. Assure that the model building directory config['model_building_code_dir']
#   exists
# 2. If there are no files in config['model_building_code_dir']:
#   - determine whether model-building code is stored in a COS bucket
#   - download model-building code to config['model_building_code_dir']
# 3. ZIP files in config['model_building_code_dir']

try:
    # task 1: make sure the specified model building code directory exists
    os.makedirs(config['model_building_code_dir'], exist_ok=True)
except Exception as ex:
    print(str(type(ex)))
    print('Error. Model building code preparation failed: {}'.format(ex))
    sys.exit(ExitCode.PRE_PROCESSING_FAILED.value)

if len(os.listdir(config['model_building_code_dir'])) == 0:
    # Task 2: try to download model building code from Cloud Object Storage
    #  bucket
    #
    print('No model building code was found in "{}".'
          .format(config['model_building_code_dir']))
    try:
        if config.get('model_bucket') is None or \
           cw.is_bucket_empty(config['model_bucket'],
                              config.get('model_key_prefix')):
            print('Error. Model building code preparation failed: '
                  'No source code was found locally in "{}" or '
                  ' in Cloud Object Storage.'
                  .format(config['model_building_code_dir']))
            sys.exit(ExitCode.PRE_PROCESSING_FAILED.value)

        print('Found model building code in bucket "{}".'
              .format(config['model_bucket']))

        for object_key in cw.get_object_list(config['model_bucket'],
                                             config.get('model_key_prefix')):
            cw.download_file(config['model_bucket'],
                             object_key,
                             config['model_building_code_dir'])
    except BucketNotFoundError as bnfe:
        print('Error. {}'.format(bnfe))
        sys.exit(ExitCode.PRE_PROCESSING_FAILED.value)
    except COSWrapperError as cwe:
        print('Error. {}'.format(cwe))
        sys.exit(ExitCode.PRE_PROCESSING_FAILED.value)
    except Exception as ex:
        print(str(ex))
        sys.exit(ExitCode.PRE_PROCESSING_FAILED.value)

print_banner('Packaging model building files in "{}" ...'
             .format(config['model_building_code_dir']))

try:
    shutil.make_archive(re.sub('.zip$', '', config['model_code_archive']),
                        'zip',
                        config['model_building_code_dir'])
except Exception as ex:
    print('Error. Packaging failed: {}'.format(str(ex)))
    sys.exit(ExitCode.PRE_PROCESSING_FAILED.value)

if os.path.isfile(config['model_code_archive']):
    # display archive content
    print('Model building package "{}" contains the following entries:'
          .format(config['model_code_archive']))
    with ZipFile(config['model_code_archive'], 'r') as archive:
        for entry in sorted(archive.namelist()):
            print(' {}'.format(entry))

    # check archive size; WML limits size to 4MB
    archive_size = os.path.getsize(config['model_code_archive'])
    archive_size_limit = 1024 * 1024 * 4
    if archive_size > archive_size_limit:
        print('Error. Your model building code archive "{}" is too large '
              '({:.2f} MB). WLM rejects archives larger than {} MB. Please '
              'remove unnecessary files from the "{}" directory and try again.'
              .format(config['model_code_archive'],
                      archive_size/(1024*1024),
                      archive_size_limit/(1024*1024),
                      config['model_building_code_dir']))
        sys.exit(ExitCode.PRE_PROCESSING_FAILED.value)


# Status:
#  - The model training job can now be started.

if cmd_parameters['command'] == 'prepare':
    print('Skipping model training and post processing steps.')
    sys.exit(ExitCode.SUCCESS.value)

# --------------------------------------------------------
# Start model training
# --------------------------------------------------------

print_banner('Starting model training ...')

client = WatsonMachineLearningAPIClient(
    {
        'url': os.environ['ML_ENV'],
        'username': os.environ['ML_USERNAME'],
        'password': os.environ['ML_PASSWORD'],
        'instance_id': os.environ['ML_INSTANCE']
    })

model_definition_metadata = {
    client.repository.DefinitionMetaNames.NAME:
        config['training_run_name'],
    client.repository.DefinitionMetaNames.DESCRIPTION:
        config['training_run_description'],
    client.repository.DefinitionMetaNames.AUTHOR_NAME:
        config['author_name'],
    client.repository.DefinitionMetaNames.FRAMEWORK_NAME:
        config['framework_name'],
    client.repository.DefinitionMetaNames.FRAMEWORK_VERSION:
        config['framework_version'],
    client.repository.DefinitionMetaNames.RUNTIME_NAME:
        config['runtime_name'],
    client.repository.DefinitionMetaNames.RUNTIME_VERSION:
        config['runtime_version'],
    client.repository.DefinitionMetaNames.EXECUTION_COMMAND:
        config['training_run_execution_command']
}

training_configuration_metadata = {
    client.training.ConfigurationMetaNames.NAME:
        config['training_run_name'],
    client.training.ConfigurationMetaNames.AUTHOR_NAME:
        config['author_name'],
    client.training.ConfigurationMetaNames.DESCRIPTION:
        config['training_run_description'],
    client.training.ConfigurationMetaNames.COMPUTE_CONFIGURATION:
        {'name': config['training_run_compute_configuration_name']},
    client.training.ConfigurationMetaNames.TRAINING_DATA_REFERENCE: {
            'connection': {
                'endpoint_url': config['cos_endpoint_url'],
                'access_key_id': os.environ['AWS_ACCESS_KEY_ID'],
                'secret_access_key': os.environ['AWS_SECRET_ACCESS_KEY']
            },
            'source': {
                'bucket': config['training_bucket'],
            },
            'type': 's3'
        },
    client.training.ConfigurationMetaNames.TRAINING_RESULTS_REFERENCE: {
            'connection': {
                'endpoint_url': config['cos_endpoint_url'],
                'access_key_id': os.environ['AWS_ACCESS_KEY_ID'],
                'secret_access_key': os.environ['AWS_SECRET_ACCESS_KEY']
            },
            'target': {
                'bucket': config['results_bucket'],
            },
            'type': 's3'
        }
}

w = WMLWrapper(client)

print('Training configuration summary:')
print(' Training run name     : {}'.format(config['training_run_name']))
print(' Training data bucket  : {}'.format(config['training_bucket']))
print(' Results bucket        : {}'.format(config['results_bucket']))
print(' Model-building archive: {}'.format(config['model_code_archive']))

training_guid = None

try:
    training_guid = w.start_training(config['model_code_archive'],
                                     model_definition_metadata,
                                     training_configuration_metadata)
except Exception as ex:
    print('Error. Model training could not be started: {}'.format(ex))
    sys.exit(ExitCode.TRAINING_FAILED.value)

print('Model training was started. Training id: {}'.format(training_guid))

# --------------------------------------------------------
# Monitor the training run until it completes
#  successfully or throws an error
# --------------------------------------------------------
#

print('Training status is updated every {} seconds - '
      '(p)ending (r)unning (e)rror (c)ompleted: '
      .format(config['training_progress_monitoring_interval']))

try:

    training_in_progress = True
    while training_in_progress:
        # poll training status; ignore server errors (e.g. caused by temporary
        # issues not specific to our training run)
        status = w.get_training_status(training_guid, ignore_server_error=True)
        if status:
            training_status = status.get('state') or '?'
        else:
            # unknown status; continue and leave it up to the user to terminate
            # monitoring
            training_status = '?'
        # display training status indicator
        #  [p]ending
        #  [r]unning
        #  [c]ompleted
        #  [e]rror
        #  [?]
        print(training_status[0:1], end='', flush=True)
        if training_status == 'completed':
            # training completed successfully
            print('\nTraining completed.')
            training_in_progress = False
        elif training_status == 'error':
            print('\nTraining failed.')
            # training ended with error
            training_in_progress = False
        else:
            time.sleep(int(config['training_progress_monitoring_interval']))
except Exception as ex:
    print('Error. Model training monitoring failed with an exception:')
    debug(' Exception type: {}'.format(type(ex)))
    debug(' Exception: {}'.format(ex))
    sys.exit(ExitCode.TRAINING_FAILED.value)

# Status:
#  - The model training job completed.
#  - The training log file TRAINING_LOG_NAME can now be downloaded from COS.

results_references = None

try:
    # --------------------------------------------------------
    # Identify where the training artifacts are stored on COS
    # {
    #   'bucket': 'ademoout3',
    #   'model_location': 'training-BA8P0BgZg'
    # }
    # Re-try to fetch information multiple times in case the WML service
    # encounters a temporary issue

    max_tries = 5
    ise = True
    for count in range(max_tries):
        results_references = \
            w.get_training_results_references(training_guid,
                                              ignore_server_error=ise)
        if results_references:
            # got a response; move on
            break
        if count + 1 == max_tries:
            # last attempt; if it fails stop trying
            ise = False

        time.sleep(3)

    # --------------------------------------------------------
    # Download the training log file from the results
    # bucket on COS to config['local_download_directory']
    # --------------------------------------------------------

    print_banner('Downloading training log file "{}" ...'
                 .format(TRAINING_LOG_NAME))

    training_log = cw.download_file(results_references['bucket'],
                                    TRAINING_LOG_NAME,
                                    config['local_download_directory'],
                                    results_references['model_location'])

    if training_status == 'error':
        # Training ended with an error. Notify the user where the training log
        # file was stored and exit.
        print('Please review the training log file "{}" in "{}"'
              .format(TRAINING_LOG_NAME,
                      config['local_download_directory']))
        sys.exit(ExitCode.TRAINING_FAILED.value)

except Exception as ex:
    print('Error. Download of training log file "{}" failed: {}'
          .format(TRAINING_LOG_NAME, ex))
    sys.exit(ExitCode.DOWNLOAD_FAILED.value)

# terminate if the "train" command was specified
# when the utility was invoked
if cmd_parameters['command'] == 'train':
    print('Skipping post-processing steps.')
    sys.exit(ExitCode.SUCCESS.value)

#  - If training completed successfully, the trained model archive
#     TRAINING_OUTPUT_ARCHIVE_NAME can now be downloaded from COS.

try:

    # --------------------------------------------------------
    # Download the trained model archive from the results
    # bucket on COS to LOCAL_DOWNLOAD_DIRECTORY
    # --------------------------------------------------------

    print_banner('Downloading trained model archive "{}" ...'
                 .format(TRAINING_OUTPUT_ARCHIVE_NAME))

    training_output = cw.download_file(results_references['bucket'],
                                       TRAINING_OUTPUT_ARCHIVE_NAME,
                                       config['local_download_directory'],
                                       results_references['model_location'])

except Exception as ex:
    print('Error. Trained model archive "{}" could not be '
          'downloaded from Cloud Object Storage bucket "{}": {}'
          .format(TRAINING_OUTPUT_ARCHIVE_NAME,
                  results_references['bucket'],
                  ex))
    sys.exit(ExitCode.DOWNLOAD_FAILED.value)

# Status:
#  - The trained model archive and training log file were
#     downloaded to the directory identified by
#     config['local_download_directory'].

# --------------------------------------------------------
# Extract the downloaded model archive
# --------------------------------------------------------

archive = os.path.join(config['local_download_directory'],
                       TRAINING_OUTPUT_ARCHIVE_NAME)

print_banner('Extracting trained model artifacts from "{}" ...'
             .format(archive))

extraction_ok = False
try:
    if tarfile.is_tarfile(archive):
        tf = tarfile.open(archive,
                          mode='r:gz')
        for file in tf.getnames():
            print(file)
        tf.extractall(config['local_download_directory'])
        print('Trained model artifacts are located in the "{}" directory.'
              .format(config['local_download_directory']))
        extraction_ok = True
    else:
        print('Error. The downloaded file "{}" is not a valid tar file.'
              .format(archive))
except FileNotFoundError:
    print('Error. "{}" was not found.'.format(archive))
except tarfile.TarError as te:
    print(te)

if extraction_ok is False:
    sys.exit(ExitCode.EXTRACTION_FAILED.value)

# Status:
#  - The trained model archive was downloaded to LOCAL_DOWNLOAD_DIRECTORY.
#    The directory structure inshould look as follows:
#     /trained_model/<framework-name-1>/<format>/<file-1>
#     /trained_model/<framework-name-1>/<format>/<file-2>
#     /trained_model/<framework-name-1>/<format-2>/<subdirectory>/<file-3>
#     /trained_model/<framework-name-2>/<file-4>

# -------------------------------------------------------------------
# Copy the appropriate framework and format specific artifacts
# to the final destination, where the Docker build will pick them up
# -------------------------------------------------------------------

trained_model_path = config['trained_model_path']
trained_assets_dir = os.path.join(config['local_download_directory'],
                                  trained_model_path)

print_banner('Copying trained model artifacts from "{}" to "{}" ...'
             .format(trained_assets_dir,
                     config['docker_model_asset_directory']))

try:
    copy_dir(trained_assets_dir,
             config['docker_model_asset_directory'])
except Exception as ex:
    print('Error. Trained model files could not be copied: {}'.format(str(ex)))
    sys.exit(ExitCode.COPY_FAILED.value)

# Status:
#  - The trained model artifacts were copied to the Docker image's asset
#    directory, where the model-serving microservice will load them from.

print('Done')
sys.exit(ExitCode.SUCCESS.value)