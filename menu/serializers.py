from rest_framework import serializers
from .models import Order, Call, Menu, Category


class OrderSerializer(serializers.ModelSerializer):
    class Meta:
        model = Order
        fields = "__all__"


class CallSerializer(serializers.ModelSerializer):
    """Serializer for Call model"""
    class Meta:
        model = Call
        fields = "__all__"


class InitiateCallSerializer(serializers.Serializer):
    """Serializer for initiating phone calls"""
    order_id = serializers.IntegerField(required=False, help_text="Associated order ID")
    phone_number = serializers.CharField(max_length=50, help_text="Customer phone number")
    context = serializers.JSONField(required=False, help_text="Additional context for the call")


class ChatTokenSerializer(serializers.Serializer):
    """Serializer for generating browser chat tokens"""
    user_context = serializers.JSONField(required=False, help_text="User context data for the session")

class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = "__all__"


class MenuSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source='category.name', read_only=True)
    
    class Meta:
        model = Menu
        fields = "__all__"