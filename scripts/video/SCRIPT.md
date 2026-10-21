# Spooldesk walkthrough — narration script

One English voice-over, about 1:50, with English and Russian subtitles. Every
scene is filmed from the static dashboard demo in `docs/demo/` (the one
published at spooldesk.net/demo/) with fictional data; "scripted AI example ·
no model called" stays visible on screen where the AI is shown, so the
narration does not have to say it.

A Russian voice was tried and rejected: Chatterbox put the stress in the wrong
places. Russian viewers get Russian subtitles instead, and the landing's
Russian page turns them on by default.

Claims checked against the product: sending a reply moves a ticket to
*waiting for the customer*; the assistant changes nothing without
confirmation; the customer agent stops when an operator takes the ticket or
the customer asks for a person.

The pipeline reads the English from `SCENES` in `narrate.py` (one entry per
sentence) and the Russian from `RU` in `render.py` (one subtitle line per
English sentence). Keep this table in step with them.

| # | Scene (what is on screen) | English (spoken) | Русский (субтитры) |
|---|---|---|---|
| 1 | Title card, channel icons | Your customers write from everywhere — email, your website, Slack, Telegram. Spooldesk brings all of it into one place, on a server you control. Let me show you how it works. | Клиенты пишут отовсюду — на почту, на сайт, в Slack, в Telegram. Spooldesk собирает всё это в одном месте, на сервере, которым управляете вы. Сейчас покажу, как это работает. |
| 2 | Ticket queue, filters, search | This is the ticket queue. Every conversation lands here, whatever channel it came from. Filter by team, category or operator — or just search for what you remember. | Это очередь обращений. Сюда попадает каждый разговор, из какого бы канала он ни пришёл. Фильтруйте по команде, категории или оператору — или просто найдите то, что помните. |
| 3 | Open a ticket, type, send; stage turns to *waiting* | Open a ticket and you get the whole story: who's asking, what they wrote, and where it came from. Type your reply right here. The moment you send it, the ticket moves to waiting for the customer — nobody has to track that by hand. | Открываете обращение — и видите всю историю: кто пишет, что пишет и откуда. Ответ пишете прямо здесь. После отправки обращение само переходит в «ждём клиента» — следить вручную не нужно. |
| 4 | Internal note | Need a colleague's help? Leave an internal note. Your team sees it. The customer never does. | Нужна помощь коллеги? Оставьте внутреннюю заметку. Команда её видит. Клиент — никогда. |
| 5 | Board | The board shows the same work by stage — new, in progress, waiting. One look, and you know what needs you next. | На доске та же работа, разложенная по стадиям: новые, в работе, ждём клиента. Один взгляд — и ясно, за что браться дальше. |
| 6 | Templates | The answers you give every day become templates. One click, instead of typing the same thing for the fortieth time. | Ответы, которые вы даёте каждый день, становятся шаблонами. Один клик — и не нужно в сороковой раз печатать одно и то же. |
| 7 | Help centre | And when an answer deserves to last, put it in the help centre. Customers find it on their own, and every article has a link you can send. | А ответ, который стоит сохранить, добавьте в базу знаний. Клиенты найдут его сами, а у каждой статьи есть ссылка, которую можно отправить. |
| 8 | Integrations | Channels are switched on in Integrations. Connect only what your team actually uses — each one comes with a checklist, so you know it really works. | Каналы включаются в разделе «Интеграции». Подключайте только нужное — у каждого канала есть чек-лист, и вы точно знаете, что он работает. |
| 9 | AI assistant: question → list; proposal → confirm | Then there's the AI assistant. Ask it in plain words — which tickets still need a reply? — and you get the answer in seconds. It can also suggest a change, like moving a ticket forward. But nothing happens until you confirm. | А дальше — ИИ-ассистент. Спросите обычными словами: каким обращениям нужен ответ? — ответ придёт за секунды. Он может и предложить изменение, например перевести обращение дальше. Но ничего не произойдёт, пока вы не подтвердите. |
| 10 | AI settings: customer agent | You can also let AI answer customers directly. Choose a single reply or a whole conversation, and set the limits. It steps aside the moment a person takes over, or the customer asks for one. | ИИ можно разрешить отвечать клиентам и напрямую. Выберите один ответ или целый диалог и задайте ограничения. ИИ уступает место, как только подключается человек или клиент просит оператора. |
| 11 | Reports | Reports show how you're doing: response times, resolution, customer satisfaction — by team and by operator. | Отчёты: время ответа и решения, оценки клиентов — по командам и операторам. |
| 12 | Closing card, spooldesk.net | Spooldesk runs on your own server, so your data stays under your control. Try the live demo at spooldesk.net — and when you're ready, install it yourself. | Spooldesk работает на вашем сервере — данные остаются под вашим контролем. Попробуйте демо на spooldesk.net, а когда будете готовы — установите у себя. |

## Spoken forms given to the voice model

The address is written out the way it should be said, so the model does not
spell it: "spooldesk dot net". The English subtitles turn it back into
"spooldesk.net".

## Words the picture is timed to

`render.py` places clicks, cuts and camera moves on words from Whisper's
transcript (`c("send")`, `c("waiting")`, `c("spooldesk", 1)` for the second
"spooldesk" in the outro, and so on). When a sentence is reworded, check that
the words its scene builder looks up are still in it.
