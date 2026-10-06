# سجل المكوّنات الخارجية ورخصها (Third-party licences)

هذا الملف يسرد كل مكوّن خارجي يستعمله نسّاخ، ورخصته، ومالك حقوقه.
شفرة نسّاخ نفسها تحت رخصة ملف `LICENSE` في جذر المستودع. كل مكوّن هنا يبقى تحت رخصته هو.

## كيف جُمع هذا السجل

- حُزم Python: قرأنا الرخصة من بيانات كل حزمة مثبّتة في `.venv` (`importlib.metadata`)، يوم 6 أكتوبر 2026.
- قائمة حُزم الخادم: `deploy/server_requirements.py` (من `pyproject.toml`) مع الإصدارات المثبّتة في `deploy/constraints.txt`.
- بيئة Kraken المنفصلة: من `.venv-kraken` على جهاز التطوير. الخادم يبني بيئة مثلها في `Dockerfile`.
- حُزم JavaScript: من حقل `license` في `package-lock.json`.
- النماذج (models): من بطاقة النموذج (model card) في Hugging Face، المحفوظة على جهاز التطوير.
- حزم النظام في صورة الخادم (system packages) والخدمات: من موقع كل مشروع. لم نقرأها من داخل الصورة.

إذا اختلف إصدار مثبّت عن هذا السجل، فالمرجع بيانات الحزمة نفسها.

## مكوّنات برخصة AGPL-3.0

- **PyMuPDF** (الإصدار 1.28.2) تحت رخصة AGPL-3.0، أو رخصة Artifex التجارية. نسّاخ يستعملها في قراءة ملفات PDF المرفوعة وتحويل صفحاتها إلى صور (`books/services.py`)، وقراءة طبقة النص في ملفات PDF الرقمية (`ocr/engines/pdf_text.py`)، وصور معاينة الصفحات (`publishing/preview.py`، `publishing/relayout.py`)، وإدراج الغلاف في مخرج PDF (`publishing/cover.py`، `publishing/pdf_export.py`).
- **EbookLib** (الإصدار 0.20) تحت رخصة AGPL-3.0-or-later. نسّاخ يستعملها في إخراج EPUB (`publishing/epub.py`).

شفرة نسّاخ متاحة للاطلاع والتقييم، وجميع الحقوق محفوظة (`LICENSE`). المكتبتان أعلاه برخصة AGPL-3.0: PyMuPDF لقراءة ملفات PDF، وEbookLib لإخراج EPUB، وتعمل بهما هذه النسخة والموقع الحي. وسنستبدلهما قبل أي خدمة تجارية مغلقة: pypdfium2 (Apache-2.0 / BSD-3) بدل PyMuPDF، ومولّد EPUB خاص بنسّاخ بدل EbookLib.

مكوّنات أخرى بشرط مشاركة (copyleft) أخف:

- psycopg تحت LGPL-3.0، وpython-bidi (بيئة Kraken) تحت LGPL. الاستيراد (import) بلا تعديل مسموح في برنامج مغلق.
- Pango وCairo وGDK-Pixbuf (مكتبات WeasyPrint في الصورة) تحت LGPL.
- صورة `redis:7` في `docker-compose.yml` تجلب Redis 7.4. رخصة Redis منذ 7.4 هي RSALv2 أو SSPLv1. الاستعمال الداخلي طابورًا (queue) مسموح. تقديم Redis نفسه خدمةً لغيرك غير مسموح. الصورة `redis:7.2` تبقى تحت BSD-3-Clause.

---

## 1. Python packages: direct dependencies (`pyproject.toml`)

"Server" means the package is in the server image (`deploy/server_requirements.py`). "Local" means only the
local PyTorch / MLX engines on the developer's Mac use it (`OCR_BACKEND=torch|mlx`).

| Package | Version | Licence (from package metadata) | Where | Use |
|---|---|---|---|---|
| Django | 6.1.1 | BSD-3-Clause | server | web framework |
| djangorestframework | 3.18.1 | BSD-3-Clause | server | JSON API |
| django-environ | 0.14.0 | MIT | server | settings from the environment |
| psycopg, psycopg-binary | 3.3.6 | LGPL-3.0-only | server | PostgreSQL driver |
| celery | 5.6.3 | BSD-3-Clause | server | task queue |
| redis (client) | 6.4.0 | MIT | server | Redis client |
| Pillow | 12.3.0 | MIT-CMU | server | images |
| **PyMuPDF** | 1.28.2 | **AGPL-3.0 or Artifex commercial licence** | server | reading PDF files, page labels, cover insertion |
| opencv-python-headless | 5.0.0.93 | Apache-2.0 | server | image processing |
| scikit-image | 0.26.0 | BSD (metadata: "BSD License") | server | image processing |
| NumPy | 2.5.3 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | server | arrays |
| RapidFuzz | 3.14.6 | MIT | server | text matching |
| pytesseract | 0.3.13 | Apache-2.0 | server | calls Tesseract |
| WeasyPrint | 70.0 | BSD (metadata: "BSD License") | server | PDF output |
| **EbookLib** | 0.20 | **AGPL-3.0-or-later** | server | EPUB output |
| python-docx | 1.2.0 | MIT | server | Word output |
| fontTools | 4.66.0 | MIT | server | font subsetting |
| httpx | 0.28.1 | BSD (metadata: "BSD License") | server | HTTP client (RunPod) |
| mcp (MCP Python SDK) | 2.3.0 | MIT | server | the MCP server |
| gunicorn | 26.2.0 | MIT (PyPI; not installed in the local `.venv`) | server | web server |
| torch | 2.14.0 | Apache-2.0 AND Apache-2.0 WITH LLVM-exception AND BSD-2-Clause AND BSD-3-Clause AND BSL-1.0 AND MIT | local | Qari on PyTorch |
| torchvision | 0.29.0 | BSD | local | Qari image input |
| transformers | 5.17.0 | Apache-2.0 | local | Qari model code |
| tokenizers | 0.23.2 | Apache-2.0 | local | (from transformers) |
| accelerate | 1.15.0 | Apache-2.0 | local | model loading |
| safetensors | 0.8.0 | Apache-2.0 | local | model weights format |
| huggingface_hub | 1.32.0 | Apache-2.0 | local | model download |
| mlx-vlm (extra `mlx`) | 0.7.2 | MIT | local (Mac) | Qari on Apple MLX |
| mlx | 0.32.2 | MIT | local (Mac) | (from mlx-vlm) |

Development only (`dev` extra, not in the server image): pytest, pytest-django, factory-boy, ruff.

## 2. Python packages: other packages in the server image (`deploy/constraints.txt`)

Packages that the direct dependencies pull in. Platform-only entries that Linux does not install (`brotlicffi`,
`httpx2-jsfetch`, `pywin32`) are left out.

| Package | Version | Licence (from package metadata) |
|---|---|---|
| amqp | 5.4.0 | BSD (metadata: "BSD License") |
| annotated-types | 0.8.0 | MIT |
| anyio | 4.15.1 | MIT |
| asgiref | 3.12.1 | BSD (metadata: "BSD License") |
| attrs | 26.1.0 | MIT |
| billiard | 4.3.0 | BSD (metadata: "BSD License") |
| brotli | 1.2.0 | MIT |
| certifi | 2026.7.22 | MPL-2.0 |
| cffi | 2.1.1 | MIT-0 |
| click | 8.5.0 | BSD-3-Clause |
| click-didyoumean | 0.3.1 | MIT |
| click-plugins | 1.1.1.2 | BSD (metadata: "BSD License") |
| click-repl | 0.4.0 | MIT |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause |
| cssselect2 | 0.10.1 | BSD (metadata: "BSD License") |
| h11 | 0.16.0 | MIT |
| httpcore | 1.0.9 | BSD-3-Clause |
| httpcore2 | 2.13.1 | BSD-3-Clause |
| httpx2 | 2.13.1 | BSD-3-Clause |
| idna | 3.20 | BSD-3-Clause |
| imageio | 2.37.4 | BSD-2-Clause |
| jsonschema | 4.26.0 | MIT |
| jsonschema-specifications | 2025.9.1 | MIT |
| kombu | 5.6.2 | BSD-3-Clause |
| lazy-loader | 0.6 | BSD-3-Clause |
| lxml | 6.1.3 | BSD-3-Clause |
| mcp-types | 2.3.0 | MIT |
| networkx | 3.7 | BSD-3-Clause |
| opentelemetry-api | 1.45.0 | Apache-2.0 |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause |
| prompt-toolkit | 3.0.53 | BSD (metadata: "BSD License") |
| pycparser | 3.0 | BSD-3-Clause |
| pydantic | 2.13.5 | MIT |
| pydantic-core | 2.46.5 | MIT |
| pydyf | 0.12.1 | BSD (metadata: "BSD License") |
| pyjwt | 2.15.1 | MIT |
| pyphen | 0.18.1 | GPL-2.0-or-later; LGPL-2.0-or-later; MPL-1.1 |
| python-dateutil | 2.9.0.post0 | BSD; Apache-2.0 |
| python-multipart | 0.0.32 | Apache-2.0 |
| referencing | 0.37.0 | MIT |
| rpds-py | 2026.6.3 | MIT |
| scipy | 1.18.1 | BSD (metadata: "BSD License") |
| six | 1.17.0 | MIT |
| sqlparse | 0.6.0 | BSD (metadata: "BSD License") |
| sse-starlette | 3.5.0 | BSD-3-Clause |
| starlette | 1.7.0 | BSD-3-Clause |
| tifffile | 2026.9.20 | BSD-3-Clause |
| tinycss2 | 1.5.1 | BSD (metadata: "BSD License") |
| tinyhtml5 | 2.1.0 | MIT |
| truststore | 0.10.4 | MIT |
| typing-extensions | 4.16.0 | PSF-2.0 |
| typing-inspection | 0.4.4 | MIT |
| tzdata | 2026.4 | Apache-2.0 |
| tzlocal | 5.4.4 | MIT |
| uvicorn | 0.53.0 | BSD-3-Clause |
| vine | 5.1.0 | BSD (metadata: "BSD License") |
| wcwidth | 0.9.1 | MIT |
| webencodings | 0.6.1 | BSD (metadata: "BSD License") |
| zopfli | 0.4.3 | Apache-2.0 |

## 3. Kraken environment (separate Python 3.11 environment)

Kraken needs older torch and NumPy, so it runs in its own environment as a subprocess (`ocr/engines/kraken.py`).
Versions from `.venv-kraken` on the developer's Mac; the server image installs `kraken>=6.0.3,<7` with CPU-only torch.

| Package | Version | Licence (from package metadata) |
|---|---|---|
| kraken | 6.0.3 | Apache-2.0 |
| torch | 2.9.0 | BSD-3-Clause |
| lightning, pytorch-lightning | 2.4.0, 2.6.6 | Apache-2.0 |
| coremltools | 8.3.0 | BSD |
| scikit-learn | 1.5.2 | BSD |
| scipy | 1.13.1 | BSD |
| shapely | 2.0.7 | BSD |
| numpy | 2.0.2 | BSD |
| pillow | 12.3.0 | MIT-CMU |
| python-bidi | 0.6.11 | LGPL |
| regex | 2026.9.10 | Apache-2.0 AND CNRI-Python |
| pyarrow | 25.0.1 | Apache-2.0 |
| protobuf | 7.36.2 | BSD-3-Clause |
| jsonschema, rich, jinja2, click, lxml, threadpoolctl | — | MIT, MIT, BSD, BSD-3-Clause, BSD-3-Clause, BSD |

## 4. JavaScript and CSS

Node.js runs only at build time (`package.json`). The browser receives three files: `static/vendor/alpine.min.js`,
`static/dist/editor.js` (Tiptap and ProseMirror, bundled by esbuild) and `static/dist/app.css` (Tailwind CSS).

| Package | Version | Licence (`package-lock.json`) | In the browser? |
|---|---|---|---|
| alpinejs | 3.17.4 | MIT | yes (`static/vendor/alpine.min.js`) |
| @tiptap/core, @tiptap/pm, @tiptap/starter-kit, @tiptap/extension-character-count, @tiptap/extension-placeholder | 3.31.3 | MIT | yes (`static/dist/editor.js`) |
| prosemirror-* (through @tiptap/pm) | see lock file | MIT | yes (`static/dist/editor.js`) |
| tailwindcss, @tailwindcss/cli | 4.3.3 | MIT | the generated CSS only |
| esbuild | 0.28.2 | MIT | no (build tool) |
| lightningcss (through Tailwind) | see lock file | MPL-2.0 | no (build tool) |
| @fontsource/ibm-plex-sans-arabic | 5.3.0 | OFL-1.1 | the font files only (section 5) |

All 141 packages in `package-lock.json`: 123 MIT, 12 MPL-2.0 (lightningcss and its platform builds), 2 Apache-2.0,
2 ISC, 1 BSD-3-Clause, 1 OFL-1.1.

## 5. Fonts

| Font | Files | Licence | Licence text in the repository |
|---|---|---|---|
| Amiri (Khaled Hosny) | `static/fonts/amiri/Amiri-Regular.ttf`, `Amiri-Bold.ttf` | SIL Open Font License 1.1 | `static/fonts/amiri/OFL.txt` |
| IBM Plex Sans Arabic (IBM Corp.) | `static/fonts/ibm-plex-sans-arabic-*.woff2` (arabic and latin, 400/500/600) | SIL Open Font License 1.1 | `static/fonts/ibm-plex-sans-arabic-OFL.txt` (copied from the npm package on 2026-10-06) |
| DejaVu (Debian `fonts-dejavu-core`) | in the server image only, a fallback face | Bitstream Vera / DejaVu free licence (upstream; not read from the image) | — |

Amiri is embedded in EPUB and PDF exports. The OFL allows this. An organisation can upload its own font face for its
books; the organisation is responsible for that font's licence.

## 6. Models

| Model | Source | Licence | Use |
|---|---|---|---|
| Qari-OCR v0.3 | Hugging Face `NAMAA-Space/Qari-OCR-v0.3-VL-2B-Instruct` | Apache-2.0 (model card) | first reader of every page region |
| Qari-OCR v0.2.2.1 | Hugging Face `NAMAA-Space/Qari-OCR-0.2.2.1-VL-2B-Instruct` (a LoRA adapter) | Apache-2.0 (model card) | second reader. The owner merged the adapter onto the base model (D7) and keeps the merged copy in a private Hugging Face repository for RunPod. |
| Qwen2-VL-2B-Instruct | Hugging Face `Qwen/Qwen2-VL-2B-Instruct` | Apache-2.0 (model card and `LICENSE` file) | base model of both Qari versions |
| Kraken `all_arabic_scripts` (OpenITI) | Zenodo record 7050270 (`https://zenodo.org/records/7050270`) | CC0-1.0 (Zenodo record; the model file itself has no licence field) | digits, footnote numbers and line boxes |
| Tesseract `ara` and `eng` (tessdata_fast) | Debian `tesseract-ocr-ara`, `tesseract-ocr-eng`; Homebrew `tesseract-lang` on the Mac | Apache-2.0 | word positions and the fallback reading |

The KITAB fine-tune of Qari (`FatimahEmadEldin/Qari-OCR-Fine-Tuned-Kitab-Benchmark`) was tested in the proof of
concept only and is not used (D14).

## 7. Programs in the server image and the containers

Licences from each upstream project, not read from the image.

| Program | Licence | Use |
|---|---|---|
| Tesseract OCR | Apache-2.0 | reading and word positions |
| PostgreSQL 17 (`postgres:17`) | PostgreSQL License | database |
| Redis (`redis:7`, now 7.4) | RSALv2 or SSPLv1 (BSD-3-Clause up to 7.2) | queue for Celery |
| Caddy 2 (`caddy:2`) | Apache-2.0 | HTTPS reverse proxy |
| Python 3.13 / 3.11 (`python:*-slim-trixie`) | PSF License | runtime |
| Pango, Cairo, GDK-Pixbuf, HarfBuzz, fontconfig | LGPL-2.1+ (Pango, GDK-Pixbuf); LGPL-2.1 or MPL-1.1 (Cairo); MIT (HarfBuzz); MIT-style (fontconfig) | WeasyPrint's drawing libraries |
| gosu | Apache-2.0 | drops root in the `init` container |
| Node.js 22 (`node:22-bookworm-slim`, build stage only) | MIT | builds the CSS and the editor bundle |

## 8. External services

| Service | Terms | Use |
|---|---|---|
| RunPod Serverless | paid service, RunPod terms of service | runs Qari on a GPU (`OCR_BACKEND=runpod`). The worker code is a separate repository (`nassakh-qari-worker`). |
| Hugging Face | Hugging Face terms of service | hosts the models; a private repository holds the merged Qari v0.2 |
| Resend | free plan, Resend terms of service | sends the sign-up confirmation emails over SMTP |
| Hostinger VPS (KVM 4) | paid service, Hostinger terms of service | the production server |
| Let's Encrypt | free certificates, Let's Encrypt subscriber agreement (through Caddy) | HTTPS certificate |

## 9. Tools used to build Nassakh

The code was written with the help of Claude Code (Anthropic), an AI coding tool. The tool is not part of the
application and is not shipped with it.
