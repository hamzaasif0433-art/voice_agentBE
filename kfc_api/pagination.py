from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response

class CustomPagination(PageNumberPagination):
    page_size = 10
    page_size_query_param = 'page_size'
    max_page_size = 100

    def get_paginated_response(self, data, data_key="data", extra_data=None):
        response_dict = {
            "success": True,
            "count": self.page.paginator.count,
            "total_pages": self.page.paginator.num_pages,
            "current_page": self.page.number,
            data_key: data,
        }
        if extra_data:
            response_dict.update(extra_data)
        return Response(response_dict)

def paginate_queryset(request, queryset, serializer_class, data_key="data", extra_data=None):
    """
    Helper function to paginate querysets inside function-based views (@api_view).
    """
    paginator = CustomPagination()
    paginated_queryset = paginator.paginate_queryset(queryset, request)
    serializer = serializer_class(paginated_queryset, many=True)
    return paginator.get_paginated_response(serializer.data, data_key=data_key, extra_data=extra_data)
