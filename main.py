"""
Manage user access by temporarily switching their Organizational Unit (OU) to a less restrictive OU.
"""

import os
import json
import logging
from datetime import datetime, timedelta, time

# Grouped google imports
from google.api_core.exceptions import (
    GoogleAPICallError,
    RetryError,
    NotFound,
    PermissionDenied,
    ServiceUnavailable,
)
from google.cloud import scheduler_v1, storage
import google.auth
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Other third-party import
from flask import jsonify
from croniter import croniter

storage_client = storage.Client()

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Constants and Configuration Handling
try:
    ADMIN_EMAIL = os.environ["ADMIN_EMAIL"]
    API_KEY = os.environ["API_KEY"]
    USER_EMAIL = os.environ["USER_EMAIL"]
    UNRESTRICTED_OU = os.environ["UNRESTRICTED_OU"]
    RESTRICTED_OU = os.environ["RESTRICTED_OU"]
    PROJECT_ID = os.environ["PROJECT_ID"]
    LOCATION = os.environ["LOCATION"]
    BUCKET_NAME = os.environ["BUCKET_NAME"]
    FILE_NAME = os.environ.get("FILE_NAME", "client_requests.json")
    UNRESTRICTED_SWITCH_LIMIT = int(
        os.environ.get("UNRESTRICTED_SWITCH_LIMIT", 3)
    )  # Number of times a user is allowed to switch to UNRESTRICTED_OU each day.
    DURATION_MINUTES = int(
        os.environ.get("DURATION_MINUTES", 30)
    )  # How many minutes until OU reverts to RESTRICTED_OU.
    # Note that the duration may be extended until DURATION_MINUTES + 59 seconds because
    # Google Cloud Scheduler works on the minute, not to the second. We round up for a
    # better user experience as rounding down the seconds would truncate time prematurely.
except KeyError as key_error:
    logger.error("Missing environment variable: %s", key_error)
    raise RuntimeError(f"Missing environment variable: {key_error}") from key_error


# Get Google Service
def get_google_service():
    """Return Google service object after authenticating using service account and admin email."""
    logger.info("Fetching Google service object.")

    # Fetch the default credentials and delegate them
    credentials, _ = google.auth.default()
    delegated_credentials = credentials.with_subject(ADMIN_EMAIL)
    
    # Build and return the service
    try:
        service = build(
            "admin",
            "directory_v1",
            credentials=delegated_credentials,
            cache_discovery=False,
        )
        logger.info("Created new Google service object.")
        return service
    except (FileNotFoundError, PermissionDenied, ServiceUnavailable,
            GoogleAPICallError, HttpError, KeyError) as error:
        logger.error("Error occurred during Google service creation: %s", error)
        raise


def check_api_key(req):
    """Check the validity of the provided API key."""
    api_key = req.headers.get("x-api-key")
    if not api_key or api_key != API_KEY:
        logger.warning("Invalid API key provided: %s", api_key)
        return False
    return True


def get_data_from_gcs():
    """
    Fetches user data from Google Cloud Storage (GCS).

    Returns:
        dict: The parsed JSON data as a dictionary, or an empty dictionary
              if the blob does not exist.
    """
    bucket = storage_client.get_bucket(BUCKET_NAME)
    blob = bucket.get_blob(FILE_NAME)

    return json.loads(blob.download_as_text()) if blob else {}


def write_data_to_gcs(user_email, user_data):
    """
    Writes or updates a user's data in Google Cloud Storage (GCS).

    Args:
        user_email (str): Email address of the user.
        user_data (dict): Data associated with the user.
    """
    current_data = get_data_from_gcs()
    current_data[user_email] = current_data.get(user_email, {})
    current_data[user_email].update(user_data)

    bucket = storage_client.get_bucket(BUCKET_NAME)
    blob = bucket.blob(FILE_NAME)
    blob.upload_from_string(json.dumps(current_data))


def get_user_ou(service, user_email):
    """Retrieve the user's Organizational Unit within Google Workspace."""
    try:
        user_info = service.users().get(userKey=user_email).execute()
        return user_info.get("orgUnitPath", None)
    except HttpError as http_error:
        logger.error("Error retrieving OU: %s", str(http_error))
        return None


def set_user_ou(service, user_email, target_ou):
    """Set a new Organizational Unit for the user within Google Workspace."""

    # Fetch current OU using get_user_ou
    current_ou = get_user_ou(service, user_email)

    # If the current OU couldn't be fetched (e.g., due to an error), return False
    if current_ou is None:
        logger.error("Failed to fetch current OU for user: %s", user_email)
        return False

    # If user is already in the desired OU, return True without making the update API call
    if current_ou == target_ou:
        logger.info("User is already in OU: %s. No changes needed.", target_ou)
        return True

    # If user's current OU is different from target_ou, proceed with update
    try:
        service.users().update(
            userKey=user_email, body={"orgUnitPath": target_ou}
        ).execute()
        logger.info("Successfully set OU to %s.", target_ou)
        return True

    except HttpError as http_error:
        logger.error("Failed to set OU to %s. Error: %s", target_ou, http_error)
    except (TypeError, AttributeError) as error:
        logger.error("Error setting OU: %s", error)
    return False


def initialize_user_data(user_email, current_date):
    """
    Initialize or reset a user's data based on current_date.

    For a new user, default values are set. For an existing user with a date
    different from the current_date, the daily request count is reset.

    Args:
        user_email (str): Email of the user.
        current_date (str): Current date in ISO format.

    Returns:
        dict: Updated user data for the given email.
    """
    users_data = get_data_from_gcs()

    # Get existing data or initialize with default values
    user_data = users_data.get(user_email)

    # If no data is found for the user, set up default values
    if not user_data:
        user_data = {
            "unrestricted_switches": 0,
            "last_request_date": current_date,
            "ou_state": RESTRICTED_OU,
            "expiration_time_utc": None,
        }

    # If a day has passed, reset unrestricted switches
    elif user_data["last_request_date"] != current_date:
        user_data["unrestricted_switches"] = 0
        user_data["last_request_date"] = current_date

    write_data_to_gcs(user_email, user_data)
    return user_data


def hours_until_midnight():
    """Returns the number of hours until midnight."""
    now = datetime.now()
    difference = datetime.combine(now + timedelta(days=1), time()) - now
    return difference.seconds // 3600


def has_exceeded_switch_limit(user_data):
    """Check if the user has exceeded the maximum allowed switches to UNRESTRICTED_OU"""
    if user_data["unrestricted_switches"] >= UNRESTRICTED_SWITCH_LIMIT:
        return True
    return False


def move_user_to_restricted_on_expiry(service, user_data, response_content):
    """
    Handle the scenario where the user is in the UNRESTRICTED_OU but the expiration time has passed.
    The user will be moved to the RESTRICTED_OU.

    Args:
        service (obj): The Google service object.
        user_data (dict): The user's data including their current OU state and expiration times.
        response_content (dict): The default response setup to be updated.

    Returns:
        tuple: Updated response_content dictionary, an HTTP status code, and the updated user_data.
    """
    if not set_user_ou(service, USER_EMAIL, RESTRICTED_OU):
        response_content.update(
            {
                "user_message": "Failed to set OU",
                "error": "Service Unavailable",
            }
        )
        return response_content, 503, user_data

    # Update user data
    user_data["ou_state"] = RESTRICTED_OU
    user_data["expiration_time_utc"] = None
    write_data_to_gcs(USER_EMAIL, user_data)

    response_content.update(
        {
            "success": True,
            "user_message": "Your access has expired and you've been moved to restricted mode.",
        }
    )
    return response_content, 200, user_data


def inform_remaining_time_in_unrestricted(
    expiration_time_utc_in_cache, user_data, response_content
):
    """
    Inform the user about the remaining time they have in the UNRESTRICTED_OU.

    Args:
        expiration_time_utc_in_cache (str): The expiration time for the user's access in ISO format.
        user_data (dict): The user's data including their current switch count.
        response_content (dict): The default response setup to be updated.

    Returns:
        tuple: Updated response_content dictionary and an HTTP status code.
    """
    remaining_time = (
        datetime.fromisoformat(expiration_time_utc_in_cache) - datetime.utcnow()
    )
    hours, remainder = divmod(remaining_time.seconds, 3600)
    minutes = remainder // 60

    # Create a list of time components and filter out zero values
    time_parts = [(hours, "hour"), (minutes, "minute")]
    time_message_parts = [
        f"{value} {unit}{'s' if value != 1 else ''}"
        for value, unit in time_parts
        if value
    ]

    time_message = " and ".join(time_message_parts)

    remaining_switches = UNRESTRICTED_SWITCH_LIMIT - user_data["unrestricted_switches"]
    switch_message = (
        f"You can switch to unrestricted mode "
        f"{remaining_switches} more time{'s' if remaining_switches != 1 else ''} today."
    )

    user_message = (
        f"You have {time_message} left in unrestricted mode. {switch_message}"
    )

    response_content.update({"success": True, "user_message": user_message})
    return response_content, 200


def transfer_user_to_unrestricted_ou(service, user_data, response_content):
    """
    Transfer the user from the RESTRICTED_OU to the UNRESTRICTED_OU and update the expiration time.

    Args:
        service (obj): The Google service object.
        user_data (dict): The user's data including their current OU state and expiration times.
        response_content (dict): The default response setup to be updated.

    Returns:
        tuple: Updated response_content dictionary, an HTTP status code, and the updated user_data.
    """

    # Attempt to set user OU
    if not set_user_ou(service, USER_EMAIL, UNRESTRICTED_OU):
        response_content.update(
            {
                "user_message": (
                    "We encountered an issue moving you to unrestricted mode. "
                    "Please try again later."
                ),
                "error": "Service Unavailable",
            }
        )
        return response_content, 503, user_data

    # Update user's switch count
    user_data["unrestricted_switches"] += 1
    write_data_to_gcs(USER_EMAIL, user_data)

    # Schedule revert job
    expiration_time_utc = schedule_revert_job()
    if not expiration_time_utc:
        response_content.update(
            {
                "user_message": (
                    "We encountered an issue scheduling your unrestricted time. "
                    "Please try again later."
                ),
                "error": "Service Unavailable",
            }
        )
        return response_content, 503, user_data

    # Update user_data with the new expiration time and OU state
    user_data.update(
        {
            "expiration_time_utc": expiration_time_utc.isoformat(),
            "ou_state": UNRESTRICTED_OU,
        }
    )
    write_data_to_gcs(USER_EMAIL, user_data)

    # Construct success response
    remaining_switches = UNRESTRICTED_SWITCH_LIMIT - user_data["unrestricted_switches"]
    times_word = "time" if remaining_switches == 1 else "times"
    user_message = (
        f"You've been moved to unrestricted mode and will be reverted "
        f"after {DURATION_MINUTES} minutes. You can switch to unrestricted mode "
        f"{remaining_switches} more {times_word} today."
    )
    response_content.update({"success": True, "user_message": user_message})

    return response_content, 200, user_data


def toggle_access(request):  # pylint: disable=too-many-return-statements
    """
    Handle access toggling for a user based on the incoming request.

    Args:
        request (obj): The incoming request object containing necessary information.

    Returns:
        tuple: A tuple containing the response content (a dictionary) and an HTTP status code.
    """

    # Default response setup
    response_content = {
        "success": False,
        "user_message": "Unknown error",
        "error": "None",
    }
    http_status = 500

    current_date = datetime.now().date().isoformat()
    user_data = initialize_user_data(USER_EMAIL, current_date)
    write_data_to_gcs(USER_EMAIL, user_data)

    if has_exceeded_switch_limit(user_data):
        hours_remaining = hours_until_midnight()
        hours_phrase = "hour" if hours_remaining == 1 else "hours"
        switches_phrase = "switch" if UNRESTRICTED_SWITCH_LIMIT == 1 else "switches"

        response_content.update(
            {
                "user_message": (
                    f"You've reached the maximum of {UNRESTRICTED_SWITCH_LIMIT} {switches_phrase} "
                    f"into a less restrictive organizational unit today. "
                    f"Try again in {hours_remaining} {hours_phrase}."
                ),
                "error": "Switch limit exceeded",
            }
        )
        http_status = 403
        return jsonify(**response_content), http_status

    try:
        if not check_api_key(request):
            response_content.update(
                {"user_message": "Unauthorized", "error": "Unauthorized"}
            )
            http_status = 401
            return jsonify(**response_content), http_status

        service = get_google_service()
        current_ou = get_user_ou(service, USER_EMAIL)
        expiration_time_utc_in_cache = user_data.get("expiration_time_utc", None)

        if current_ou == UNRESTRICTED_OU:
            # Scenario 1: User in UNRESTRICTED_OU but no expiration time
            if expiration_time_utc_in_cache is None:
                logging.info("user_in_unrestricted_without_expiration.")
                (
                    response_content,
                    http_status,
                    user_data,
                ) = move_user_to_restricted_on_expiry(
                    service, user_data, response_content
                )
                return jsonify(**response_content), http_status

            # Scenario 2: User in UNRESTRICTED_OU with expired time
            if datetime.utcnow().isoformat() > expiration_time_utc_in_cache:
                logging.info(
                    "user_in_unrestricted_expired_time: expiration_time: %s",
                    expiration_time_utc_in_cache,
                )
                (
                    response_content,
                    http_status,
                    user_data,
                ) = move_user_to_restricted_on_expiry(
                    service, user_data, response_content
                )
                return jsonify(**response_content), http_status

            # Scenario 3: User in UNRESTRICTED_OU with valid time remaining
            if datetime.utcnow().isoformat() <= expiration_time_utc_in_cache:
                logging.info(
                    "user_in_unrestricted_valid_time_remaining: expiration_time: %s",
                    expiration_time_utc_in_cache,
                )
                response_content, http_status = inform_remaining_time_in_unrestricted(
                    expiration_time_utc_in_cache, user_data, response_content
                )
                return jsonify(**response_content), http_status

        # Scenario 4: User is in the restricted OU and needs to be moved to the unrestricted OU.
        else:
            logging.info("user_in_restricted_moving_to_unrestricted.")
            response_content, http_status, user_data = transfer_user_to_unrestricted_ou(
                service, user_data, response_content
            )
            return jsonify(**response_content), http_status

    except (
        HttpError,
        GoogleAPICallError,
        RetryError,
        NotFound,
        PermissionDenied,
        ServiceUnavailable,
    ) as google_api_error:
        logger.error("Google API error: %s", str(google_api_error))
        response_content.update(
            {"user_message": "Google API error", "error": "Google API error"}
        )
        http_status = 503

    return jsonify(**response_content), http_status


# Scheduler Functions
def get_job_name(user_email):
    """Return the job name based on USER_EMAIL."""
    sanitized_email = user_email.replace("@", "_").replace(".", "_")
    return (
        f"projects/{PROJECT_ID}/locations/{LOCATION}/jobs/{sanitized_email}_revert_ou"
    )


def schedule_revert_job():
    """Schedule a job to revert the OU after DURATION_MINUTES and update the cache."""

    client = scheduler_v1.CloudSchedulerClient()

    # Calculate when the next job should run and adjust scheduled_time to round up
    # to the nearest minute for buffer. We do this because Google Cloud Scheduler
    # only runs by the minute, not to the second.
    scheduled_time = datetime.utcnow() + timedelta(minutes=DURATION_MINUTES)
    scheduled_time = scheduled_time.replace(second=0, microsecond=0) + timedelta(
        minutes=1
    )

    try:
        existing_job = client.get_job(name=get_job_name(USER_EMAIL))
        cron_schedule_str = existing_job.schedule
        cron_iter = croniter(cron_schedule_str, datetime.utcnow())
        existing_scheduled_time = cron_iter.get_next(datetime)

        # If the existing job runs after the duration, delete it
        time_difference = (
            existing_scheduled_time - datetime.utcnow()
        ).total_seconds() / 60
        if time_difference > DURATION_MINUTES:
            delete_scheduler_job(USER_EMAIL)
            # We don't return after deleting. We let it proceed to create a new job.
        else:
            logger.info(
                "Revert job for user already exists and is scheduled to run at %s. Job name: %s.",
                existing_scheduled_time,
                existing_job.name,
            )
            return existing_scheduled_time
    except NotFound:
        pass  # Continue to create a new job if no existing job or if the previous one was deleted

    # Set up HTTP target and cron schedule
    http_target = {
        "uri": f"https://{LOCATION}-{PROJECT_ID}.cloudfunctions.net/cron_revert_ou",
        "http_method": scheduler_v1.HttpMethod.POST,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"email": USER_EMAIL}).encode("utf-8"),
    }
    cron_schedule = f"{scheduled_time.minute} {scheduled_time.hour} * * *"
    job = {
        "name": get_job_name(USER_EMAIL),
        "http_target": http_target,
        "schedule": cron_schedule,
    }

    try:
        response = client.create_job(
            parent=f"projects/{PROJECT_ID}/locations/{LOCATION}", job=job
        )
        logger.info(
            "Scheduled new revert job for user at %s. Job name: %s.",
            scheduled_time,
            response.name,
        )
        return scheduled_time
    except (GoogleAPICallError, RetryError) as error:
        logger.error("Failed to schedule a new revert job for user. Error: %s.", error)
        return None


def cron_revert_ou(request):
    """Revert the Organizational Unit (OU) for a user whose unrestricted time has expired."""

    # Verify HTTP method
    if request.method != "POST":
        logging.error("Function called with wrong HTTP method: %s", request.method)
        return jsonify(error="This function expects a POST request"), 405

    # Initialize service and fetch user data
    service = get_google_service()
    users_data = get_data_from_gcs()
    user_data = users_data.get(USER_EMAIL)
    current_ou = get_user_ou(service, USER_EMAIL)

    # Check conditions for reverting OU
    if (
        user_data.get("ou_state") == UNRESTRICTED_OU
        and datetime.utcnow().isoformat() >= user_data.get("expiration_time_utc")
    ) or (
        current_ou == UNRESTRICTED_OU and user_data.get("ou_state") != UNRESTRICTED_OU
    ):
        logging.info("Condition met to revert OU.")
        response_content, http_status, user_data = move_user_to_restricted_on_expiry(
            service, user_data, {}
        )
        if http_status == 200:
            delete_scheduler_job(USER_EMAIL)
            message = "Successfully reverted OU for user."
            logging.info(message)
        else:
            error_msg = (
                f"Error processing user. Expected OU (from local cache): "
                f"{user_data.get('ou_state')}, Actual OU (from Google service): {current_ou}. "
                f"Error message: {response_content['user_message']}"
            )
            logging.error(error_msg)
            return jsonify(error=response_content["user_message"]), http_status
    else:
        message = (
            "User is either not in UNRESTRICTED_OU or unrestricted time hasn't elapsed."
        )
        logging.warning(message)

    # Reset request count for new day
    current_date = datetime.now().date().isoformat()
    if user_data.get("last_request_date") != current_date:
        logging.info("Resetting request count for the new day.")
        user_data["unrestricted_switches"] = 0
        user_data["last_request_date"] = current_date
        write_data_to_gcs(USER_EMAIL, user_data)

    return (
        jsonify(success=user_data.get("ou_state") == RESTRICTED_OU, message=message),
        200,
    )


def delete_scheduler_job(user_email):
    """Delete a Cloud Scheduler job."""
    client = scheduler_v1.CloudSchedulerClient()
    job_name = get_job_name(user_email)

    try:
        client.delete_job(name=job_name)
        logger.info("Successfully deleted Cloud Scheduler job.")
    except NotFound:
        pass
    except Exception as error:
        logger.error("Failed to delete Cloud Scheduler job: %s", error)
        raise
