from django.contrib import admin
from .models import GeminiSessionCost, CallHistory

@admin.register(GeminiSessionCost)
class GeminiSessionCostAdmin(admin.ModelAdmin):
    list_display = (
        'session_id', 'agent_type',
        'input_text_tokens', 'input_audio_tokens',
        'output_text_tokens', 'output_audio_tokens',
        'total_tokens', 'estimated_cost_usd',
        'call_duration_seconds', 'created_at',
    )
    list_filter = ('agent_type', 'created_at')
    search_fields = ('session_id',)
    readonly_fields = ('created_at', 'updated_at')

@admin.register(CallHistory)
class CallHistoryAdmin(admin.ModelAdmin):
    list_display = ('session_id', 'agent_type', 'duration_seconds', 'created_at')
    list_filter = ('agent_type', 'created_at')
    search_fields = ('session_id',)
    readonly_fields = ('created_at',)
