# pytest-snapshot-django-orm
Тесты django-приложений без лишних обращений к БД

## Описание проблемы
Мой стек: django + DRF + srf_spectacular
Минимальное view в таком стеке:
``` python
class UserViewSet(mixins.CreateModelMixin, viewsets.GenericViewSet):
    queryset = User.objects.all()
    serializer_class = UserSerializer
```
Проблема в том, что при тестировании api на основе такого view, тест проверяет не только логику view, но все middleware и взаимодействия с БД.
Если middleware проходит достаточно, то взаимодействие с БД - медленное. Как следствие, когда количество тестов приближается к 1000, то время выполнения тестов становится некомфортным.
