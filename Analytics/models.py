from django.db import models

class GeminiSessionCost(models.Model):
    session_id = models.CharField(max_length=255, unique=True, help_text="Unique session identifier for the voice call")
    agent_type = models.CharField(max_length=50, help_text="The type of agent, e.g., healthcare, restaurant")
    prompt_tokens = models.IntegerField(default=0, help_text="Number of prompt tokens used")
    response_tokens = models.IntegerField(default=0, help_text="Number of response tokens used")
    total_tokens = models.IntegerField(default=0, help_text="Total tokens used in this session")
    input_audio_tokens = models.IntegerField(default=0, help_text="Number of input audio tokens (user speech)")
    output_audio_tokens = models.IntegerField(default=0, help_text="Number of output audio tokens (agent speech)")
    call_duration_seconds = models.IntegerField(default=0, help_text="Duration of the voice call in seconds")
    estimated_cost_usd = models.DecimalField(max_digits=10, decimal_places=6, default=0.000000, help_text="Estimated cost based on $3 per 1M input / $12 per 1M output audio tokens")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.agent_type} Session {self.session_id} - {self.total_tokens} tokens"

class CallHistory(models.Model):
    session_id = models.CharField(max_length=255, unique=True, help_text="Unique session identifier matching GeminiSessionCost")
    agent_type = models.CharField(max_length=50, help_text="The type of agent, e.g., healthcare, restaurant")
    duration_seconds = models.IntegerField(default=0, help_text="Duration of the WebSocket connection in seconds")
    transcript = models.JSONField(default=list, help_text="List of JSON objects representing the conversation turns")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.agent_type} Call {self.session_id} - {self.duration_seconds}s"

