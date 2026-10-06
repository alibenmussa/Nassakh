# مجلد التجارب (playground)

هذا المجلد يحفظ تجارب بناء نسّاخ. هو دليل على طريقة العمل: كل قرار في `docs/DECISIONS.md` قام على قياس هنا.

## ما ليس فيه

- هذا المجلد ليس جزءًا من التطبيق. شفرة التطبيق لا تستورد منه شيئًا.
- استثناء واحد على جهاز المالك: الإعداد `OCR_MODELS_DIR` في `.env.example` يشير إلى `playground/poc/models`. هذا المجلد يحفظ أوزان النماذج (model weights)، و`.gitignore` يستثنيه.
- خادم الإنتاج لا يحتاج هذا المجلد. ملف `.dockerignore` يستثنيه من الصورة (image)، وملف `pyproject.toml` يستثنيه من الفحص (ruff).
- السكربتات هنا كُتبت لجهاز المالك ولقاعدة بيانات التطوير. بعضها لا يعمل بدونهما.

## صور الصفحات حُذفت

- حذفنا صور صفحات الكتب ومقاطعها (crops) يوم 6 أكتوبر 2026، قبل نشر المستودع.
- السبب: الكتب التي جُرّبت عليها محفوظة الحقوق (copyright). لا نملك حق نشر صورها.
- ملف `.gitignore` يمنع إضافة صور أو ملفات PDF تحت هذا المجلد.
- التقارير ما زالت تذكر أسماء الصور. الصورة نفسها غير موجودة.
- الصور الباقية في `cover/` صور اصطناعية (مستطيل ودائرة). ليست من أي كتاب.
- الصور تبقى في سجل git القديم (git history). الوسم `challenge-baseline-2026-10-03` يشير إليه، لذلك لم نعدّل السجل.

## محتوى المجلد

| المجلد | ما فيه |
|---|---|
| `poc/` | إثبات المفهوم (proof of concept) للقراءة الآلية (OCR): مقارنة Qari v0.3 وv0.2 وKITAB وTesseract على 17 صفحة من أربعة كتب. التقرير في `poc/REPORT.md`. |
| `poc/gt/` | النص الصحيح (ground truth) للصفحات الـ17، صحّحه إنسان. يُستعمل لحساب نسبة الخطأ في الحروف (CER). |
| `poc/runs/` | ناتج كل نموذج على كل صفحة (JSON)، وملخصه في `poc/runs_summary.csv`. |
| `digits/` | قراءة الأرقام العربية الهندية (٠١٢٣٤٥٦٧٨٩): Tesseract وQari وKraken على 222 مقطعًا. التقرير في `digits/REPORT.md`. |
| `phase7/` | تجارب التخطيط (layout) والحواشي وعلامات الشك (flags)، ومذكرات التصميم. انظر `phase7/README.md`. |
| `cover/` | تجربة الغلاف (cover) في WeasyPrint وPyMuPDF. انظر `cover/README.md`. |
| `word/` | تجربة التصدير إلى Word: هل يخرج Word الصفحة كما تخرجها المعاينة؟ التقرير في `word/REPORT.md`. |

بعض المجلدات تظهر على جهاز المالك فقط، مثل `footnotes-*` و`fullbook-*` و`runpod-*`. ملف `.gitignore` يستثني ملفاتها الكبيرة وصورها.

## كيف تقرأ هذا المجلد

1. ابدأ بملف `REPORT.md` في كل مجلد. فيه السؤال والعيّنة والنتيجة.
2. ارجع إلى `docs/DECISIONS.md` لترى القرار الذي بُني على النتيجة.
3. لا تشغّل السكربتات على خادم الإنتاج.

---

## English summary

This folder holds the experiments behind Nassakh: the OCR proof of concept, digit reading, layout and footnote
probes, the cover spike and the Word export spike. The app code does not import it, and the server image excludes
it. On the owner's Mac, `OCR_MODELS_DIR` points to the ignored `playground/poc/models` folder for the model weights.
The page images and crops of the test books were removed on 2026-10-06 because the books are under copyright;
`.gitignore` blocks new images and PDFs here. They remain in the old git history, which was not rewritten so that the
baseline tag keeps its hash.
