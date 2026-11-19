# Обновления бота графиков бассейна

Эта ветка служит только для раздачи обновлений pool-bot.
Бот читает `latest.json` и по кнопке «🔄 Aktualizuj» скачивает указанный там zip, сверяя sha256.
Здесь нет данных сотрудников, `.env` и токена.

`latest-beta.json` is the test channel: every change lands there first and is checked on the
separate test bot (UPDATE_URL pointing at latest-beta.json). Only the changes chosen after testing
are released through `latest.json`.
