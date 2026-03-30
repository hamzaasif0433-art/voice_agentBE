from rest_framework import serializers
from .models import GeminiSessionCost, CallHistory

class GeminiSessionCostSerializer(serializers.ModelSerializer):
    """Serializer for tracking Gemini session costs and token usage."""
    class Meta:
        model = GeminiSessionCost
        fields = "__all__"

class CallHistorySerializer(serializers.ModelSerializer):
    """Serializer for detailed voice call history including transcripts."""
    # We can include the cost data if it exists for this session
    cost_data = serializers.SerializerMethodField()

    class Meta:
        model = CallHistory
        fields = ["id", "session_id", "agent_type", "duration_seconds", "transcript", "created_at", "cost_data"]

    def get_cost_data(self, obj):
        try:
            cost = GeminiSessionCost.objects.filter(session_id=obj.session_id).first()
            if cost:
                return GeminiSessionCostSerializer(cost).data
        except Exception:
            pass
        return None
