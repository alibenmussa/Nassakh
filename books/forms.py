"""New-book form: metadata, the PDF and the ingest options, with Arabic labels and messages."""

from __future__ import annotations

from django import forms

import pymupdf

from books.models import Book
from books.services import text_layer_enabled

MAX_PDF_MB = 500
INPUT = {"class": "input"}


class BookForm(forms.ModelForm):
    """Create a book from a PDF upload plus skip/split options (spec screen 3).

    The PDF is checked by extension, size and by actually opening it with PyMuPDF, which also
    gives the page count used to validate the skip values.
    """

    class Meta:
        model = Book
        fields = [
            "title",
            "author",
            "original_year",
            "notes",
            "source_pdf",
            "skip_first",
            "skip_last",
            "pages_per_sheet",
            "split_ratio",
            "use_text_layer",
        ]
        widgets = {
            "title": forms.TextInput(attrs={**INPUT, "autofocus": True, "maxlength": 300}),
            "author": forms.TextInput(attrs={**INPUT, "maxlength": 300}),
            "original_year": forms.NumberInput(
                attrs={**INPUT, "min": 1, "max": 2100, "dir": "ltr", "inputmode": "numeric"}
            ),
            "notes": forms.Textarea(attrs={**INPUT, "rows": 3}),
            "source_pdf": forms.ClearableFileInput(attrs={**INPUT, "accept": "application/pdf,.pdf"}),
            "skip_first": forms.NumberInput(
                attrs={**INPUT, "min": 0, "max": 500, "dir": "ltr", "inputmode": "numeric"}
            ),
            "skip_last": forms.NumberInput(
                attrs={**INPUT, "min": 0, "max": 500, "dir": "ltr", "inputmode": "numeric"}
            ),
            "pages_per_sheet": forms.RadioSelect(),
            "split_ratio": forms.NumberInput(
                attrs={"type": "range", "class": "range", "min": "0.3", "max": "0.7", "step": "0.01"}
            ),
            "use_text_layer": forms.CheckboxInput(),
        }
        labels = {
            "source_pdf": "ملف PDF",
            "skip_first": "تجاوز الصفحات الأولى",
            "skip_last": "تجاوز الصفحات الأخيرة",
            "pages_per_sheet": "صفحات في كل ورقة",
            "split_ratio": "موضع القص",
            "use_text_layer": "استخدام الطبقة النصية إن وُجدت",
        }
        help_texts = {
            "title": "كما يظهر على صفحة العنوان.",
            "original_year": "سنة الطبعة الأصلية بالأرقام، مثل 1966. اختياري.",
            "notes": "ملاحظات داخلية عن النسخة أو المصدر. اختيارية.",
            "source_pdf": (
                f"ملف PDF واحد للكتاب كاملًا، حتى {MAX_PDF_MB} ميغابايت. "
                "المسح الضوئي والملفات الرقمية مقبولان."
            ),
            "skip_first": "عدد الصفحات في بداية الملف التي لا تُعالَج (الأغلفة وصفحات العنوان).",
            "skip_last": "عدد الصفحات في نهاية الملف التي لا تُعالَج (الغلاف الخلفي مثلًا).",
            "pages_per_sheet": (
                "اختر «صفحتان» عندما تحوي كل صورة في الملف صفحتين متجاورتين من الكتاب؛ "
                "تُقصّ الورقة إلى صفحة يمنى ثم يسرى."
            ),
            "split_ratio": (
                "موضع الخط الفاصل بين الصفحتين نسبةً إلى عرض الورقة من الحافة اليسرى. "
                "يُكتشف الفاصل تلقائيًا قرب هذا الموضع، ويُستخدم الموضع نفسه عند تعذّر الاكتشاف."
            ),
            "use_text_layer": (
                "للكتب الرقمية: يُستخرج النص من الملف مباشرة ويُقارَن بنتيجة التعرّف الضوئي "
                "بدل الاعتماد على التعرّف وحده."
            ),
        }
        error_messages = {
            "title": {"required": "أدخل عنوان الكتاب."},
            "source_pdf": {"required": "اختر ملف PDF.", "invalid": "الملف المرفوع غير صالح."},
            "original_year": {"invalid": "أدخل السنة بالأرقام."},
            "skip_first": {"invalid": "أدخل عددًا صحيحًا.", "min_value": "لا يمكن أن يكون العدد سالبًا."},
            "skip_last": {"invalid": "أدخل عددًا صحيحًا.", "min_value": "لا يمكن أن يكون العدد سالبًا."},
            "pages_per_sheet": {"required": "اختر عدد الصفحات في كل ورقة.", "invalid_choice": "اختر 1 أو 2."},
            "split_ratio": {"invalid": "أدخل نسبة بين 0.2 و0.8."},
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["source_pdf"].required = True
        self.fields["pages_per_sheet"].required = True
        self.fields["split_ratio"].required = False
        if text_layer_enabled():
            self.fields["use_text_layer"].required = False
        else:
            # Off (item 20): the option is not offered and a posted value is ignored; the book keeps the
            # model default (False) and its text comes from OCR.
            del self.fields["use_text_layer"]
        self._pdf_page_count: int | None = None

    # ------------------------------------------------------------------ field validation

    def clean_source_pdf(self):
        pdf = self.cleaned_data.get("source_pdf")
        if not pdf:
            return pdf
        name = (pdf.name or "").lower()
        if not name.endswith(".pdf"):
            raise forms.ValidationError("يُقبل ملف PDF فقط (الامتداد .pdf).")
        if pdf.size is not None and pdf.size > MAX_PDF_MB * 1024 * 1024:
            raise forms.ValidationError(
                f"حجم الملف يتجاوز {MAX_PDF_MB} ميغابايت. اضغط الملف أو قسّمه ثم أعد المحاولة."
            )
        if pdf.size == 0:
            raise forms.ValidationError("الملف فارغ.")

        try:
            doc = _open_uploaded_pdf(pdf)
        except Exception as exc:  # noqa: BLE001 - PyMuPDF raises several types for bad files
            raise forms.ValidationError(
                "تعذّر فتح الملف بوصفه PDF. تأكد أن الملف سليم ثم أعد المحاولة."
            ) from exc
        try:
            if doc.needs_pass:
                raise forms.ValidationError("الملف محمي بكلمة مرور. أزل الحماية ثم أعد رفعه.")
            self._pdf_page_count = doc.page_count
        finally:
            doc.close()
        if not self._pdf_page_count:
            raise forms.ValidationError("الملف لا يحوي أي صفحة.")
        return pdf

    def clean_split_ratio(self):
        ratio = self.cleaned_data.get("split_ratio")
        if ratio in (None, ""):
            return 0.5
        if not 0.2 <= ratio <= 0.8:
            raise forms.ValidationError("أدخل نسبة بين 0.2 و0.8.")
        return ratio

    def clean(self):
        cleaned = super().clean()
        skip_first = cleaned.get("skip_first") or 0
        skip_last = cleaned.get("skip_last") or 0
        if self._pdf_page_count and skip_first + skip_last >= self._pdf_page_count:
            self.add_error(
                "skip_first",
                f"الملف يحوي {self._pdf_page_count} صفحة؛ "
                f"قيم التجاوز ({skip_first} + {skip_last}) لا تترك أي صفحة.",
            )
        return cleaned

    @property
    def pdf_page_count(self) -> int | None:
        """Page count read while validating the upload (None before validation)."""
        return self._pdf_page_count


def _open_uploaded_pdf(pdf) -> pymupdf.Document:
    """Open an uploaded file with PyMuPDF without consuming it (temp path when Django spooled it)."""
    temp_path = getattr(pdf, "temporary_file_path", None)
    if callable(temp_path):
        return pymupdf.open(temp_path())
    pdf.seek(0)
    data = pdf.read()
    pdf.seek(0)
    return pymupdf.open(stream=data, filetype="pdf")
