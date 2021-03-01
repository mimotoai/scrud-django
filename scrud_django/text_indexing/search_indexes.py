from haystack import indexes

from ..models import Resource


class ResourceIndex(indexes.SearchIndex, indexes.Indexable):
    text = indexes.CharField(document=True, use_template=True)

    def get_model(self):
        return Resource

    def index_queryset(self, using=None):
        return self.get_model().objects.all()

    def prepare(self, object):
        self.prepared_data = super(ResourceIndex, self).prepare(object)

        # self.prepared_data['content'] =
        # [content.text for text in object.content.all()]

        return self.prepared_data
