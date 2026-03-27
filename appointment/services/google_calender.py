# appointments/services/google_calendar.py

from google.oauth2 import service_account
from googleapiclient.discovery import build
from datetime import datetime, timedelta
import pytz
import os
import json
from pathlib import Path

SCOPES = ['https://www.googleapis.com/auth/calendar']
CALENDAR_ID = os.getenv('GOOGLE_CALENDAR_ID', 'alihassan9682@gmail.com')
TIMEZONE = 'Asia/Karachi'


def get_calendar_service():
    """Build and return Google Calendar service using GOOGLE_SERVICE_ACCOUNT_JSON"""
    _raw_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not _raw_json:
        raise EnvironmentError("GOOGLE_SERVICE_ACCOUNT_JSON environment variable is not set.")

    try:
        _sa_info = json.loads(_raw_json)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in GOOGLE_SERVICE_ACCOUNT_JSON: {e}") from e

    # Fix Railway's double-escaped newlines in private_key
    if "private_key" in _sa_info:
        _sa_info["private_key"] = _sa_info["private_key"].replace("\\n", "\n")

    creds = service_account.Credentials.from_service_account_info(
        _sa_info,
        scopes=SCOPES
    )
    return build('calendar', 'v3', credentials=creds, cache_discovery=False)


def create_meeting(appointment):
    """
    Creates a Google Calendar event with Meet link
    from an Appointment instance
    """
    service = get_calendar_service()

    # Combine date + time into datetime
    start_dt = datetime.combine(appointment.date, appointment.start_time)
    end_dt   = datetime.combine(appointment.date, appointment.end_time)

    # Guard against empty time range (start == end or end < start)
    if end_dt <= start_dt:
        end_dt = start_dt + timedelta(minutes=30)

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
        'reminders': {
            'useDefault': False,
            'overrides': [
                {'method': 'email', 'minutes': 60},   # 1 hr before
                {'method': 'popup', 'minutes': 15},   # 15 min before
            ]
        }
    }

    # Try creating event WITH Google Meet link first
    try:
        event_with_meet = {**event, 'conferenceData': {
            'createRequest': {
                'requestId': f'appt-{appointment.id}',
                'conferenceSolutionKey': {'type': 'hangoutsMeet'},
            }
        }}
        result = service.events().insert(
            calendarId=CALENDAR_ID,
            body=event_with_meet,
            conferenceDataVersion=1,
            sendUpdates='none',
        ).execute()
    except Exception as meet_err:
        # Meet not supported (free Gmail) — fall back to plain calendar event
        print(f"Meet link not available, creating plain event: {meet_err}")
        result = service.events().insert(
            calendarId=CALENDAR_ID,
            body=event,
            conferenceDataVersion=0,
            sendUpdates='none',
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