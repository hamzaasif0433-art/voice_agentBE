# appointments/services/google_calendar.py

from google.oauth2 import service_account
from googleapiclient.discovery import build
from datetime import datetime, timedelta
import pytz
import os
import json
from pathlib import Path

SCOPES = ['https://www.googleapis.com/auth/calendar']
# Resolve credentials from environment first, then fallback to local files.
_THIS_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _THIS_DIR.parent.parent
_env_creds = os.getenv('GOOGLE_APPLICATION_CREDENTIALS') or os.getenv('GOOGLE_SERVICE_ACCOUNT_FILE')

if _env_creds:
    _creds_path = Path(os.path.expandvars(_env_creds)).expanduser()
    SERVICE_ACCOUNT_FILE = str(_creds_path if _creds_path.is_absolute() else (_PROJECT_ROOT / _creds_path).resolve())
else:
    local_creds = _THIS_DIR / 'credentials.json'
    root_creds = _PROJECT_ROOT / 'service-account.json'
    SERVICE_ACCOUNT_FILE = str(local_creds if local_creds.exists() else root_creds)

CALENDAR_ID = 'alihassan9682@gmail.com'
TIMEZONE = 'Asia/Karachi'


def get_calendar_service():
    """Build and return Google Calendar service"""
    credentials_json = os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON')

    # Backward compatibility: accept JSON directly in GOOGLE_APPLICATION_CREDENTIALS.
    if not credentials_json:
        raw_gac = os.getenv('GOOGLE_APPLICATION_CREDENTIALS', '').strip()
        if raw_gac.startswith('{'):
            credentials_json = raw_gac

    if credentials_json:
        creds = service_account.Credentials.from_service_account_info(
            json.loads(credentials_json),
            scopes=SCOPES
        )
    else:
        creds = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE,
            scopes=SCOPES
        )

    return build('calendar', 'v3', credentials=creds)


def create_meeting(appointment):
    """
    Creates a Google Calendar event with Meet link
    from an Appointment instance
    """
    service = get_calendar_service()

    # Combine date + time into datetime
    start_dt = datetime.combine(appointment.date, appointment.start_time)
    end_dt   = datetime.combine(appointment.date, appointment.end_time)

    event = {
        'summary': f'Appointment - {appointment.name}',
        'description': appointment.notes or '',
        'start': {
            'dateTime': start_dt.isoformat(),
            'timeZone': TIMEZONE,
        },
        'end': {
            'dateTime': end_dt.isoformat(),
            'timeZone': TIMEZONE,
        },
        # 'attendees': [
        #     {'email': appointment.email},   # customer
        #     # {'email': 'your@email.com'},  # optionally add host
        # ],
        'conferenceData': {
            'createRequest': {
                'requestId': f'appt-{appointment.id}',  # unique per event
                'conferenceSolutionKey': {'type': 'eventHangout'},
            }
        },
        'reminders': {
            'useDefault': False,
            'overrides': [
                {'method': 'email', 'minutes': 60},   # 1 hr before
                {'method': 'popup', 'minutes': 15},   # 15 min before
            ]
        }
    }

    result = service.events().insert(
        calendarId=CALENDAR_ID,
        body=event,
        conferenceDataVersion=0,  # ← change from 1 to 0
        sendUpdates='none',          # sends email invites to attendees
    ).execute()

    return {
        'event_id':  result.get('id'),
        'meet_link': result.get('conferenceData', {})
                           .get('entryPoints', [{}])[0]
                           .get('uri'),
        'calendar_link': result.get('htmlLink')
    }


def cancel_meeting(event_id):
    """Delete/cancel a calendar event"""
    service = get_calendar_service()
    service.events().delete(
        calendarId=CALENDAR_ID,
        eventId=event_id,
        sendUpdates='all'
    ).execute()