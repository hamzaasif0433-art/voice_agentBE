# analytics/urls.py
from django.urls import path
from Analytics.views import order_stats, RevenuePerformance, SalesDistribution, GeminiCostStatsView, GeminiCallHistoryView
from Analytics.views.Webhooks import ElevenLabsWebhookView

urlpatterns = [
    path('order_stats/', order_stats.as_view(), name='order_stats'),
    path('Revenue_Performance/', RevenuePerformance.as_view(), name='RevenuePerformance'),
    path('Sales_Distribution/', SalesDistribution.as_view(), name='SalesDistribution'),
    path('gemini-costs/', GeminiCostStatsView.as_view(), name='gemini_costs'),
    path('gemini-history/', GeminiCallHistoryView.as_view(), name='gemini_history'),
    path('webhooks/elevenlabs/', ElevenLabsWebhookView.as_view(), name='elevenlabs_webhook'),
]

