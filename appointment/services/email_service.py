from django.core.mail import send_mail
from django.conf import settings


def send_appointment_email(appointment):
    """
    Sends an appointment confirmation email to the customer
    with meeting details and Google Meet link.
    """
    if not appointment.email:
        return

    calendar_link = appointment.calendar_link or 'Not generated'

    subject = f"Appointment Confirmation - {appointment.date.strftime('%B %d, %Y')}"

    message = f"""Hello {appointment.name},

Your appointment has been confirmed. Here are your details:

  Date       : {appointment.date.strftime('%A, %B %d, %Y')}
  Time       : {appointment.start_time.strftime('%I:%M %p')} - {appointment.end_time.strftime('%I:%M %p')}
  Status     : {appointment.status.capitalize()}
  Calendar   : {calendar_link}
"""

    if appointment.meet_link:
        message += f"  Google Meet: {appointment.meet_link}\n"

    if appointment.notes:
        message += f"\n  Notes      : {appointment.notes}\n"

    message += """
If you need to cancel or reschedule, please contact us as soon as possible.

Thank you,
The Team
"""

    send_mail(
        subject=subject,
        message=message,
        from_email=settings.EMAIL_HOST_USER,
        recipient_list=[appointment.email],
        fail_silently=False,
    )
