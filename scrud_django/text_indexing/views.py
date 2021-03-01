from haystack.query import EmptySearchQuerySet, SearchQuerySet
from rest_framework import viewsets

from ..serializers import ResourceSerializer


class ResourceSearchViewSet(viewsets.ViewSet):
    serializer_class = ResourceSerializer

    def get_queryset(self):
        request = self.request
        queryset = EmptySearchQuerySet()

        if request.GET.get("q", ""):
            query = request.GET.get("q", "")
            # Do i need to filter by json expressions using json-path
            queryset = SearchQuerySet().filter(content=query)
        return queryset

    def create(self, request):
        pass

    def retrieve(self, request, pk=None):
        pass

    def update(self, request, pk=None):
        pass

    def destroy(self, request, pk=None):
        pass
