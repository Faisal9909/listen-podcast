# استمع — بودكاست من بريدك

كل ساعة يقرأ GitHub Actions رسائل Gmail الجديدة، ويحوّلها إلى صوت بأصوات Microsoft Edge العصبية (`ar-SA-HamedNeural`)،
وينشرها كبودكاست على GitHub Pages تحت رابط سري. تشترك فيه مرة واحدة في تطبيق **Podcasts** على الآيفون، وتصلك الحلقات تلقائيًا.

## الإعداد (مرة واحدة)

1. **كلمة مرور تطبيقات Gmail**: فعّل التحقق بخطوتين، ثم أنشئ كلمة مرور من <https://myaccount.google.com/apppasswords>.
2. **أسرار الريبو** (Settings ← Secrets and variables ← Actions ← Secrets):
   - `GMAIL_ADDRESS`: عنوان Gmail
   - `GMAIL_APP_PASSWORD`: كلمة مرور التطبيقات (16 حرفًا)
   - `FEED_TOKEN`: نص عشوائي طويل، وهو المسار السري للبودكاست
3. **Pages**: Settings ← Pages ← Source = **GitHub Actions**.
4. **الأتمتة**: اجعل أداة الذكاء الاصطناعي ترسل رسائلها إلى `اسمك+listen@gmail.com`، وتصلك عادي في بريدك.
5. شغّل الـ workflow يدويًا مرة: Actions ← Listen podcast ← Run workflow، واكتب نصًا تجريبيًا.
6. افتح على الآيفون: `https://faisal9909.github.io/listen-podcast/<FEED_TOKEN>/`، واضغط «إضافة إلى تطبيق Podcasts».

## إعدادات اختيارية (Variables)

| المتغير | الافتراضي |
|---|---|
| `GMAIL_QUERY` | `to:اسمك+listen@gmail.com newer_than:30d` (أي بحث Gmail بحروف إنجليزية، مثل `from:x@y.com` أو `label:listen`) |
| `VOICE_AR` | `ar-SA-HamedNeural` (أو `ar-SA-ZariyahNeural` لصوت أنثوي) |
| `VOICE_EN` | `en-US-AndrewMultilingualNeural` |
| `TTS_RATE` | `+0%` (مثلًا `+10%` للتسريع) |

ملاحظة: سجلات Actions في الريبو العام مكشوفة، لذلك السكربت لا يطبع محتوى الرسائل أبدًا. والرابط السري يحمي الحلقات من التخمين، لكن من يحصل عليه يقدر يسمعها.
