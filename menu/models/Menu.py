from django.db import models
from .Category import Category

class Menu(models.Model):
    name = models.CharField(max_length=200)
    cost = models.IntegerField()
    category = models.ForeignKey(Category, on_delete=models.SET_NULL, null=True, blank=True, related_name='items')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        cat_name = self.category.name if self.category else "Uncategorized"
        return f"Menu Item #{self.id} - [{cat_name}] {self.name} cost :{self.cost}"
