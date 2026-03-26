from django.shortcuts import render
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from django.views.decorators.csrf import csrf_exempt
from .models import Schedule,Appointment
from .serializers import ScheduleSerializer
from .serializers import AppointmentSerializer
from .services.google_calender import create_meeting, cancel_meeting
from .services.email_service import send_appointment_email
from rest_framework.views import APIView
from datetime import datetime, timedelta, date as today_date

@csrf_exempt
@api_view(['GET', 'POST', 'PATCH'])
def schedule(request):
    if request.method == "GET":
        schedules = Schedule.objects.all()
        serializer = ScheduleSerializer(schedules, many=True)
        return Response({
            "success": True,
            "data": serializer.data
        }, status=status.HTTP_200_OK)

    if request.method == "POST":
        serializer = ScheduleSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response({
                "success": True,
                "message": "Schedule created successfully",
                "data": serializer.data
            }, status=status.HTTP_201_CREATED)
        return Response({
            "success": False,
            "errors": serializer.errors
        }, status=status.HTTP_400_BAD_REQUEST)

    if request.method == "PATCH":
        day = request.data.get("day_of_week")  

        if not day:
            return Response({
                "success": False,
                "message": "Day is required for update"
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            instance = Schedule.objects.get(day_of_week=day)  # fetch the specific day
        except Schedule.DoesNotExist:
            return Response({
                "success": False,
                "message": f"No schedule found for day: {day}"
            }, status=status.HTTP_404_NOT_FOUND)

        serializer = ScheduleSerializer(instance, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response({
                "success": True,
                "message": "Schedule updated successfully",
                "data": serializer.data
            }, status=status.HTTP_200_OK)
        return Response({
            "success": False,
            "errors": serializer.errors
        }, status=status.HTTP_400_BAD_REQUEST)

class AppointmentCreateView(APIView):

    def post(self, request):
        # 0. Idempotency Check: Handle Gemini issuing duplicate tool calls on timeout
        date_str = request.data.get('date')
        start_time_str = request.data.get('start_time')
        phone = request.data.get('phone')

        if date_str and start_time_str and phone:
            existing = Appointment.objects.filter(
                date=date_str,
                start_time=start_time_str,
                phone=phone
            ).first()
            if existing:
                # Return 200 OK with the already created appointment
                return Response(
                    AppointmentSerializer(existing).data,
                    status=status.HTTP_200_OK
                )

        serializer = AppointmentSerializer(data=request.data)

        if serializer.is_valid():
            # Extract appointment details for validation
            appointment_date = serializer.validated_data.get('date')
            start_time = serializer.validated_data.get('start_time')
            end_time = serializer.validated_data.get('end_time')

            # Check if the date is not in the past
            from datetime import date
            if appointment_date < date.today():
                return Response({
                    "success": False,
                    "message": "Invalid appointment date",
                    "error": "Appointment date cannot be in the past. Please select today or a future date."
                }, status=status.HTTP_400_BAD_REQUEST)

            # Check if the time slot is available
            overlapping_appointments = Appointment.objects.filter(
                date=appointment_date,
                status__in=['pending', 'confirmed']  # Only check active appointments
            ).filter(
                # Check for time overlap: new slot starts before existing ends AND new slot ends after existing starts
                start_time__lt=end_time,
                end_time__gt=start_time
            )

            if overlapping_appointments.exists():
                conflicting_slot = overlapping_appointments.first()
                return Response({
                    "success": False,
                    "message": "Time slot not available",
                    "error": f"This time slot conflicts with an existing appointment from {conflicting_slot.start_time.strftime('%H:%M')} to {conflicting_slot.end_time.strftime('%H:%M')}",
                    "conflicting_appointment": {
                        "date": conflicting_slot.date,
                        "start_time": conflicting_slot.start_time,
                        "end_time": conflicting_slot.end_time
                    }
                }, status=status.HTTP_400_BAD_REQUEST)

            # If slot is available, proceed with booking
            appointment = serializer.save()  # save to DB first

            try:
                calendar_data = create_meeting(appointment)
                appointment.google_event_id = calendar_data['event_id']
                appointment.meet_link        = calendar_data['meet_link']
                appointment.calendar_link    = calendar_data['calendar_link']
                appointment.save()
            except Exception as e:
                # Don't fail the booking if calendar fails
                print(f"Calendar error: {e}")

            try:
                send_appointment_email(appointment)
            except Exception as e:
                # Don't fail the booking if email fails
                print(f"Email error: {e}")

            return Response(AppointmentSerializer(appointment).data,
                            status=status.HTTP_201_CREATED)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class AppointmentCancelView(APIView):

    def patch(self, request, pk):
        appointment = Appointment.objects.get(pk=pk)
        appointment.status = 'cancelled'
        appointment.save()

        # Cancel the Google Calendar event too
        if appointment.google_event_id:
            cancel_meeting(appointment.google_event_id)

        return Response({'message': 'Appointment cancelled'})


class AppointmentListView(APIView):

    def get(self, request):
        appointments = Appointment.objects.all().order_by('date', 'start_time')

        # Optional filters
        status_filter = request.query_params.get('status')
        date_filter = request.query_params.get('date')

        if status_filter:
            appointments = appointments.filter(status=status_filter)

        if date_filter:
            try:
                date = datetime.strptime(date_filter, '%Y-%m-%d').date()
            except ValueError:
                return Response(
                    {'error': 'Invalid date format. Use YYYY-MM-DD'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            appointments = appointments.filter(date=date)

        serializer = AppointmentSerializer(appointments, many=True)
        return Response({
            'success': True,
            'count': appointments.count(),
            'data': serializer.data
        }, status=status.HTTP_200_OK)


class AvailableSlotsView(APIView):
    
    def get(self, request):
        date_str = request.query_params.get('date')  

        # 1. Validate date param exists
        if not date_str:
            return Response(
                {'error': 'Date parameter is required. Use format YYYY-MM-DD'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # 2. Parse date string
        try:
            date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            return Response(
                {'error': 'Invalid date format. Use YYYY-MM-DD'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # 3. Check if date is not in the past
        if date < today_date.today():
            return Response(
                {'error': 'Date cannot be in the past.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # 4. Get schedule for that day
        day_of_week = date.weekday()  # 0=Mon, 6=Sun
        try:
            schedule = Schedule.objects.get(day_of_week=day_of_week, is_active=True)
        except Schedule.DoesNotExist:
            return Response(
                {'error': 'No schedule available for this day'},
                status=status.HTTP_404_NOT_FOUND
            )

        all_slots = []
        current = datetime.combine(date, schedule.start_time)
        end     = datetime.combine(date, schedule.end_time)

        while current + timedelta(minutes=schedule.slot_duration) <= end:
            slot_end = current + timedelta(minutes=schedule.slot_duration)
            all_slots.append({
                'start': current.strftime('%H:%M'),
                'end':   slot_end.strftime('%H:%M'),
            })
            current += timedelta(minutes=schedule.slot_duration)

        booked = Appointment.objects.filter(
            date=date,
            status__in=['pending', 'confirmed']
        ).values_list('start_time', flat=True)

        booked_times = [t.strftime('%H:%M') for t in booked]

        # 5. Filter out booked slots
        available_slots = [
            slot for slot in all_slots
            if slot['start'] not in booked_times
        ]

        return Response({
            'date':            date_str,
            'day':             schedule.get_day_of_week_display(),
            'slot_duration':   f"{schedule.slot_duration} mins",
            'total_slots':     len(all_slots),
            'booked_slots':    len(booked_times),
            'available_slots': len(available_slots),
            'slots':           available_slots
        })