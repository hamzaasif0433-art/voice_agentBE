from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from django.views.decorators.csrf import csrf_exempt
from .serializers import OrderSerializer, InitiateCallSerializer, ChatTokenSerializer, CallSerializer, MenuSerializer, CategorySerializer
from .models import Order, Call, Menu, Category
from .services import ElevenLabsService
import uuid


@csrf_exempt
@api_view(['GET', 'POST', 'DELETE'])
def menu(request):
    """Return the KFC menu items"""
    if request.method == "POST":
        serializer = MenuSerializer(data=request.data)

        if serializer.is_valid():
            serializer.save()
            return Response({
                "success": True,
                "message": "Menu item created successfully",
                "data": serializer.data
            }, status=status.HTTP_201_CREATED)

        return Response({
            "success": False,
            "errors": serializer.errors
        }, status=status.HTTP_400_BAD_REQUEST)

    if request.method == "DELETE":
        menu_id = request.query_params.get('id')
        if not menu_id:
            return Response({
                "success": False,
                "message": "Menu item ID is required"
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            menu_id = int(menu_id)
            menu_item = Menu.objects.get(id=menu_id)
            menu_item.delete()
            return Response({
                "success": True,
                "message": "Menu item deleted successfully"
            }, status=status.HTTP_200_OK)
        except ValueError:
            return Response({
                "success": False,
                "message": "Menu item ID must be a valid number"
            }, status=status.HTTP_400_BAD_REQUEST)
        except Menu.DoesNotExist:
            return Response({
                "success": False,
                "message": "Menu item not found"
            }, status=status.HTTP_404_NOT_FOUND)

    if request.method == "GET":
        data = Menu.objects.all()
        serializer = MenuSerializer(data, many=True)

        return Response({
            "success": True,
            "menu": serializer.data
        }, status=status.HTTP_200_OK)


@csrf_exempt
@api_view(['GET', 'POST', 'DELETE'])
def categories(request):
    """Manage Menu Categories"""
    if request.method == "POST":
        serializer = CategorySerializer(data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response({
                "success": True,
                "message": "Category created successfully",
                "data": serializer.data
            }, status=status.HTTP_201_CREATED)
        return Response({
            "success": False,
            "errors": serializer.errors
        }, status=status.HTTP_400_BAD_REQUEST)

    if request.method == "DELETE":
        cat_id = request.query_params.get('id')
        if not cat_id:
            return Response({"success": False, "message": "ID required"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            category = Category.objects.get(id=int(cat_id))
            category.delete()
            return Response({"success": True, "message": "Category deleted"}, status=status.HTTP_200_OK)
        except (ValueError, Category.DoesNotExist):
            return Response({"success": False, "message": "Invalid ID or not found"}, status=status.HTTP_400_BAD_REQUEST)

    if request.method == "GET":
        data = Category.objects.all()
        serializer = CategorySerializer(data, many=True)
        return Response({
            "success": True,
            "categories": serializer.data
        }, status=status.HTTP_200_OK)


@csrf_exempt
@api_view(["GET", "POST"])
def orders(request):
    # POST create new order
    if request.method == "POST":
        serializer = OrderSerializer(data=request.data)

        if serializer.is_valid():
            serializer.save()
            return Response({
                "success": True,
                "message": "Order created successfully",
                "order": serializer.data
            }, status=status.HTTP_201_CREATED)

        return Response({
            "success": False,
            "errors": serializer.errors
        }, status=status.HTTP_400_BAD_REQUEST)
    if request.method == "GET":
        orders = Order.objects.all()
        serializer = OrderSerializer(orders, many=True)

        return Response({
            "success": True,
            "orders": serializer.data
        }, status=status.HTTP_200_OK)


@csrf_exempt
@api_view(["GET"])
def call_list(request):
    """
    List all calls with optional filters.

    GET /calls/
    Query params:
      ?status=completed|initiated|ongoing|failed
      ?call_type=browser|outbound|inbound
    """
    calls = Call.objects.all()

    status_filter    = request.query_params.get('status')
    call_type_filter = request.query_params.get('call_type')

    if status_filter:
        calls = calls.filter(status=status_filter)

    if call_type_filter:
        calls = calls.filter(call_type=call_type_filter)

    serializer = CallSerializer(calls, many=True)
    return Response({
        "success": True,
        "count": calls.count(),
        "data": serializer.data
    }, status=status.HTTP_200_OK)


@csrf_exempt
@api_view(["POST"])
def initiate_call(request):
    """
    Initiate an outbound phone call to a customer using ElevenLabs agent.

    POST /calls/initiate/
    Body:
    {
        "order_id": 123 (optional),
        "phone_number": "+923001234567",
        "context": {"order_details": "..."} (optional)
    }
    """
    serializer = InitiateCallSerializer(data=request.data)

    if not serializer.is_valid():
        return Response({
            "success": False,
            "message": "Invalid request data",
            "errors": serializer.errors
        }, status=status.HTTP_400_BAD_REQUEST)

    validated_data = serializer.validated_data
    phone_number = validated_data.get('phone_number')
    order_id = validated_data.get('order_id')
    context = validated_data.get('context', {})

    # Initialize ElevenLabs service
    elevenlabs = ElevenLabsService()

    # Initiate the phone call
    result = elevenlabs.initiate_phone_call(phone_number, context)

    if not result['success']:
        return Response({
            "success": False,
            "message": f"Failed to initiate call: {result.get('error', 'Unknown error')}",
            "data": None
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    # Save call record to database
    order_instance = None
    if order_id:
        try:
            order_instance = Order.objects.get(id=order_id)
        except Order.DoesNotExist:
            pass

    call = Call.objects.create(
        order=order_instance,
        phone_number=phone_number,
        call_type='outbound',
        conversation_id=result['conversation_id'],
        status=result['status'],
        metadata=context
    )

    return Response({
        "success": True,
        "message": "Phone call initiated successfully",
        "data": {
            "conversation_id": call.conversation_id,
            "status": call.status,
            "phone_number": phone_number,
            "call_id": call.id
        }
    }, status=status.HTTP_201_CREATED)


@csrf_exempt
@api_view(["POST"])
def get_chat_token(request):
    """
    Generate a signed token for browser-based voice chat.

    POST /calls/chat-token/
    Body:
    {
        "user_context": {"customer_name": "John", "cart": [...]} (optional)
    }
    """
    serializer = ChatTokenSerializer(data=request.data)

    if not serializer.is_valid():
        return Response({
            "success": False,
            "message": "Invalid request data",
            "errors": serializer.errors
        }, status=status.HTTP_400_BAD_REQUEST)

    validated_data = serializer.validated_data
    user_context = validated_data.get('user_context', {})
    requested_agent_id = request.data.get('agent_id')

    # Initialize ElevenLabs service
    elevenlabs = ElevenLabsService()

    # Get signed token for chat
    result = elevenlabs.get_signed_token_for_chat(
        user_context=user_context,
        agent_id=requested_agent_id
    )

    if not result['success']:
        http_status = result.get('status_code', status.HTTP_500_INTERNAL_SERVER_ERROR)
        return Response({
            "success": False,
            "message": result.get('user_message', f"Failed to generate chat token: {result.get('error', 'Unknown error')}"),
            "error_code": result.get('error_code', 'unknown_error'),
            "error": result.get('error', 'Unknown error'),
            "data": None
        }, status=http_status)

    # Create a call record for browser chat
    conversation_id = str(uuid.uuid4())
    call = Call.objects.create(
        phone_number='',
        call_type='browser',
        conversation_id=conversation_id,
        status='initiated',
        metadata=user_context
    )

    return Response({
        "success": True,
        "message": "Chat token generated successfully",
        "data": {
            "signed_url": result['signed_url'],
            "agent_id": result['agent_id'],
            "expires_at": result.get('expires_at'),
            "conversation_id": conversation_id,
            "call_id": call.id
        }
    }, status=status.HTTP_200_OK)


@csrf_exempt
@api_view(["POST"])
def elevenlabs_webhook(request):
    """
    Webhook handler for ElevenLabs call status updates.

    POST /calls/webhook/
    Body: (from ElevenLabs)
    {
        "conversation_id": "...",
        "status": "completed",
        "transcript": "...",
        "duration": 123
    }
    """
    data = request.data
    conversation_id = data.get('conversation_id')

    if not conversation_id:
        return Response({
            "success": False,
            "message": "Missing conversation_id"
        }, status=status.HTTP_400_BAD_REQUEST)

    try:
        call = Call.objects.get(conversation_id=conversation_id)

        # Update call record with webhook data
        if 'status' in data:
            call.status = data['status']
        if 'transcript' in data:
            call.transcript = data['transcript']
        if 'duration' in data:
            call.duration_seconds = data['duration']

        call.save()

        return Response({
            "success": True,
            "message": "Webhook processed successfully"
        }, status=status.HTTP_200_OK)

    except Call.DoesNotExist:
        return Response({
            "success": False,
            "message": "Call not found"
        }, status=status.HTTP_404_NOT_FOUND)


@csrf_exempt
@api_view(["GET"])
def call_status(request, conversation_id):
    """
    Get the status of a specific call.

    GET /calls/status/<conversation_id>/
    """
    try:
        call = Call.objects.get(conversation_id=conversation_id)

        # Optionally fetch latest status from ElevenLabs
        elevenlabs = ElevenLabsService()
        latest_status = elevenlabs.get_conversation_status(conversation_id)

        # Update local record if we got new data
        if latest_status['success']:
            if latest_status.get('status'):
                call.status = latest_status['status']
            if latest_status.get('transcript'):
                call.transcript = latest_status['transcript']
            if latest_status.get('duration_seconds'):
                call.duration_seconds = latest_status['duration_seconds']
            call.save()

        serializer = CallSerializer(call)

        return Response({
            "success": True,
            "message": "Call status retrieved successfully",
            "data": serializer.data
        }, status=status.HTTP_200_OK)

    except Call.DoesNotExist:
        return Response({
            "success": False,
            "message": "Call not found",
            "data": None
        }, status=status.HTTP_404_NOT_FOUND)