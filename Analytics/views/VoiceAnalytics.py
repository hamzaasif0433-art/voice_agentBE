from django.utils import timezone
from django.db.models import Sum, Count, Avg
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from ..models import GeminiSessionCost, CallHistory
from ..serializers import GeminiSessionCostSerializer, CallHistorySerializer

class GeminiCostStatsView(APIView):
    """
    API view to return aggregated Gemini cost and token statistics.
    """
    def get(self, request, *args, **kwargs):
        today = timezone.now().date()
        
        # Aggregates
        total_stats = GeminiSessionCost.objects.aggregate(
            total_usd=Sum('estimated_cost_usd'),
            total_tokens=Sum('total_tokens'),
            total_calls=Count('id'),
            avg_duration=Avg('call_duration_seconds')
        )
        
        today_stats = GeminiSessionCost.objects.filter(created_at__date=today).aggregate(
            today_usd=Sum('estimated_cost_usd'),
            today_tokens=Sum('total_tokens'),
            today_calls=Count('id')
        )
        
        # Breakdown by agent type
        agent_breakdown = GeminiSessionCost.objects.values('agent_type').annotate(
            cost=Sum('estimated_cost_usd'),
            calls=Count('id')
        ).order_by('-cost')

        # Recent costs (last 7 days)
        last_7_days = []
        for i in range(6, -1, -1):
            date = today - timezone.timedelta(days=i)
            day_stats = GeminiSessionCost.objects.filter(created_at__date=date).aggregate(
                cost=Sum('estimated_cost_usd')
            )
            last_7_days.append({
                "date": date.isoformat(),
                "cost": float(day_stats['cost'] or 0)
            })

        data = {
            "total_usd": float(total_stats['total_usd'] or 0),
            "total_tokens": total_stats['total_tokens'] or 0,
            "total_calls": total_stats['total_calls'] or 0,
            "avg_duration_seconds": float(total_stats['avg_duration'] or 0),
            "today_usd": float(today_stats['today_usd'] or 0),
            "today_tokens": today_stats['today_tokens'] or 0,
            "today_calls": today_stats['today_calls'] or 0,
            "agent_breakdown": agent_breakdown,
            "cost_history": last_7_days
        }
        
        return Response(data)

class GeminiCallHistoryView(APIView):
    """
    API view to return list of call history from Gemini sessions.
    """
    def get(self, request, *args, **kwargs):
        # We want to list all CallHistory records, joined with their cost if available
        calls = CallHistory.objects.all().order_by('-created_at')
        
        # Support filtering by agent
        agent_type = request.query_params.get('agent_type')
        if agent_type:
            calls = calls.filter(agent_type=agent_type)
            
        serializer = CallHistorySerializer(calls, many=True)
        
        return Response({
            "success": True,
            "count": calls.count(),
            "data": serializer.data
        })
