# فهرس مجلد التوثيق (docs)

الملفات في ثلاث مجموعات. كل ملف في سطر واحد.
أكثر الملفات بالإنجليزية. ملفات التحدي والمصادر بالعربية.

## 1. عام

ملفات تصف النظام كما هو اليوم.

| الملف | ما فيه |
|---|---|
| [`DECISIONS.md`](DECISIONS.md) | سجل القرارات المرقّمة (D1 إلى D112)، وسبب كل قرار. |
| [`RUN_LOCALLY.md`](RUN_LOCALLY.md) | تشغيل نسّاخ على جهازك: Mac مع MLX، أو Docker على المعالج (CPU). |
| [`RUNBOOK.md`](RUNBOOK.md) | تشغيل نسّاخ على جهاز Mac للتطوير، والأوامر، ونقاط الواجهة البرمجية (API). |
| [`DEPLOY.md`](DEPLOY.md) | خطوات نشر نسّاخ على خادم خاص افتراضي (VPS) بـ Docker Compose. |
| [`SOURCES.md`](SOURCES.md) | توثيق المحتوى والمصادر: الكتب، وقاعدة الحقوق، وطريقة التحقق من النص، والحدود. |
| [`THIRD_PARTY_LICENSES.md`](THIRD_PARTY_LICENSES.md) | سجل المكوّنات الخارجية ورخصها: الحزم، والخطوط، والنماذج، والخدمات. |

## 2. المرحلة الأولى قبل التحدي (baseline)

مواصفات نسخة البداية وخططها وتقاريرها ومراجعاتها، حتى 3 أكتوبر 2026. المجلد `baseline/`.

| الملف | ما فيه |
|---|---|
| [`PLAN.md`](baseline/PLAN.md) | خطة البناء الأصلية ومراحلها. |
| [`PHASE2_SPEC.md`](baseline/PHASE2_SPEC.md) | المرحلة 2: مسار المعالجة (processing pipeline). |
| [`PHASE3_SPEC.md`](baseline/PHASE3_SPEC.md) | المرحلة 3: شاشة متابعة المعالجة وشاشة المراجعة. |
| [`PHASE4_SPEC.md`](baseline/PHASE4_SPEC.md) | المرحلة 4: تجميع الصفحات المراجَعة في مخطوطة واحدة (assembly). |
| [`PHASE5_SPEC.md`](baseline/PHASE5_SPEC.md) | المرحلة 5: المحرّر، وتنسيق الكتاب، ومعاينة الصفحة. |
| [`PHASE6_SPEC.md`](baseline/PHASE6_SPEC.md) | المرحلة 6: التصدير إلى Word وPDF وEPUB. |
| [`PHASE7_SPEC.md`](baseline/PHASE7_SPEC.md) | المرحلة 7: «التخطيط» ثم «المعالجة»، والذهاب والعودة الآمن، والثقة في النص. |
| [`DASHBOARD_SPEC.md`](baseline/DASHBOARD_SPEC.md) | صفحة الكتاب لوحةً تعكس صفحاته الممسوحة. |
| [`COVER_SPEC.md`](baseline/COVER_SPEC.md) | الغلاف (cover): صورة أو نص، في كل مخرج (D80). |
| [`NOTE_CALLS_SPEC.md`](baseline/NOTE_CALLS_SPEC.md) | قراءة أرقام إحالات الحواشي من الحبر (D83، D87). |
| [`RUNPOD_SPEC.md`](baseline/RUNPOD_SPEC.md) | تشغيل Qari على RunPod Serverless بوحدة GPU (`OCR_BACKEND=runpod`). |
| [`FULLBOOK_TEST_2026-09-28.md`](baseline/FULLBOOK_TEST_2026-09-28.md) | اختبار كتاب كامل، الأول: 294 صفحة من الرفع إلى التصدير. |
| [`FULLBOOK_TEST_2026-09-30.md`](baseline/FULLBOOK_TEST_2026-09-30.md) | اختبار كتاب كامل، الثاني: كتاب حديث مشكول من 120 صفحة. |
| [`FOOTNOTES_REPORT_2026-09-30.md`](baseline/FOOTNOTES_REPORT_2026-09-30.md) | تقرير الحواشي: الإحالات التي وُجدت ورُبطت. |
| [`UX_TEST_2026-09-26.md`](baseline/UX_TEST_2026-09-26.md) | اختبار يدوي بعين ناشر ومصمم واجهات. |
| [`REVIEW_PHASE7_2026-09-27.md`](baseline/REVIEW_PHASE7_2026-09-27.md) | مراجعة سريعة للمرحلة 7 قبل اختبار المالك. |
| [`TODO_REVIEW_2026-10-03.md`](baseline/TODO_REVIEW_2026-10-03.md) | ملاحظات المالك على المسار كاملًا، مترجمة ومجمّعة. |

## 3. أعمال التحدي 4–6 أكتوبر 2026

ما بُني في أيام التحدي، من الوسم `challenge-baseline-2026-10-03`. المجلد `challenge/`.

| الملف | ما فيه |
|---|---|
| [`CHALLENGE_BASELINE.md`](challenge/CHALLENGE_BASELINE.md) | نسخة البداية (baseline) قبل أيام التحدي، ومكوّناتها، والوسم `challenge-baseline-2026-10-03`. |
| [`CHALLENGE_SPEC.md`](challenge/CHALLENGE_SPEC.md) | ما بُني في أيام التحدي: الحسابات والرصيد، والبحث والتحقق من الاقتباس، وخادم MCP (MCP server). |
| [`CHALLENGE_RESULTS.md`](challenge/CHALLENGE_RESULTS.md) | قياس التحقق من الاقتباس: النتائج، والمقارنة مع البحث البسيط، وطريقة إعادة التشغيل، والحدود. |
| [`DEPLOY_REPORT_2026-10-05.md`](challenge/DEPLOY_REPORT_2026-10-05.md) | تقرير النشر الأول على nassakh.tech: ما اختُبر، وما تعطّل وأُصلح، وما بقي. |
| [`DEMO_DATA.md`](challenge/DEMO_DATA.md) | نقل الكتب المعالَجة من جهاز التطوير إلى الخادم (حزمة التصدير والاستيراد). |
