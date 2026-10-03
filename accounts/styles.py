"""An organisation's format templates (D98): a named set of a stylesheet's values, taken from one book and
applied to others.

A template holds the book's stylesheet (`editor.services.stylesheet_values` over `STYLESHEET_FIELDS`, so a
field the stylesheet gains later is carried too) without what belongs to the book alone: `updated_at`, the
book details of the title and copyright pages (`front_matter.fields`) and the cover (`front_matter.cover`).
The trim, the margins and the bleed, the three faces, the sizes, the leading, the indent, the heading
sizes, the footnotes, the page furniture, widows and orphans and which front pages print are all in it.

Applying a template goes through the stylesheet's own validation and save (`editor.services
.update_stylesheet`), so a book gets exactly what the «التنسيق» panel would give it; a face of the template
that its organisation removed since is left out (the book keeps its own) and said (`skipped`). Before
applying, `template_changes` lists what changes, field by field, in the panel's words.
"""

from __future__ import annotations

from django.db import IntegrityError, transaction

from .models import StyleTemplate

NAME_MAX = 120
DESCRIPTION_MAX = 300
FACE_FIELDS: tuple[str, ...] = ("body_font", "latin_font", "heading_font")
FACE_ROLES: dict[str, str] = {"body_font": "body", "latin_font": "latin", "heading_font": "heading"}

NO_NAME = "اكتب اسمًا للقالب."
NAME_TOO_LONG = f"الاسم أطول من {NAME_MAX} حرفًا."
DESCRIPTION_TOO_LONG = f"الوصف أطول من {DESCRIPTION_MAX} حرفًا."
NAME_TAKEN = "في المؤسسة قالب بهذا الاسم؛ اختر اسمًا آخر، أو حدّث القالب نفسه."
OTHER_ORGANIZATION = "هذا الكتاب لا ينتمي إلى مؤسسة القالب."
NO_ORGANIZATION = "هذا الكتاب لا ينتمي إلى مؤسسة؛ لا قوالب له."

# the panel's words for each field (a field without one is named by the model's verbose name)
LABELS: dict[str, str] = {
    "trim": "القطع",
    "width_mm": "العرض",
    "height_mm": "الارتفاع",
    "top_mm": "الهامش العلوي",
    "bottom_mm": "الهامش السفلي",
    "inner_mm": "الهامش الداخلي",
    "outer_mm": "الهامش الخارجي",
    "bleed_mm": "زيادة القص",
    "body_font": "خط المتن",
    "latin_font": "الخط اللاتيني والأرقام",
    "heading_font": "خط العناوين",
    "body_size_pt": "حجم المتن",
    "line_height": "تباعد الأسطر",
    "indent_em": "إزاحة أول السطر",
    "footnote_size_pt": "حجم الحواشي",
    "heading_scale.h1": "حجم عنوان الفصل",
    "heading_scale.h2": "حجم العنوان الفرعي",
    "footnote_numbering": "ترقيم الحواشي",
    "running_header": "الترويسة",
    "page_number": "رقم الصفحة",
    "chapter_opening": "بداية الفصل",
    "widows": "أقل عدد من الأسطر أعلى الصفحة",
    "orphans": "أقل عدد من الأسطر أسفل الصفحة",
    "keep_headings": "العنوان لا يُفصل عن نصّه",
    "print_source_pages": "أرقام الصفحات الأصلية في الهامش",
    "front_matter.title_page": "صفحة العنوان",
    "front_matter.contents": "صفحة المحتويات",
    "front_matter.copyright_page": "صفحة الحقوق",
}
# a field the stylesheet gains later with plain words for values (an alignment, a side) is said in Arabic
WORDS: dict[str, str] = {
    "right": "يمين",
    "left": "يسار",
    "center": "وسط",
    "justify": "ضبط",
    "start": "البداية",
    "end": "النهاية",
    "none": "بلا",
}
UNITS: dict[str, str] = {
    "width_mm": "مم",
    "height_mm": "مم",
    "top_mm": "مم",
    "bottom_mm": "مم",
    "inner_mm": "مم",
    "outer_mm": "مم",
    "bleed_mm": "مم",
    "body_size_pt": "نقطة",
    "footnote_size_pt": "نقطة",
    "indent_em": "em",
    "heading_scale.h1": "×",
    "heading_scale.h2": "×",
    "widows": "سطر",
    "orphans": "سطر",
}


class TemplateError(ValueError):
    """A refused template action (the message is Arabic)."""


# ====================================================================== values


def template_values(sheet) -> dict:
    """The stylesheet's values a template keeps (see the module docstring)."""
    from editor.services import FRONT_FLAGS, stylesheet_values

    values = stylesheet_values(sheet)
    values.pop("updated_at", None)
    front = values.get("front_matter") if isinstance(values.get("front_matter"), dict) else {}
    values["front_matter"] = {key: bool(front.get(key, default)) for key, default in FRONT_FLAGS.items()}
    return values


def book_values(book) -> dict:
    """`template_values` of a book's stylesheet (its defaults when it has none)."""
    from editor.services import stylesheet_for

    return template_values(stylesheet_for(book))


def _flatten(values: dict, prefix: str = "") -> dict:
    out: dict = {}
    for key, value in (values or {}).items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            out.update(_flatten(value, path))
        else:
            out[path] = value
    return out


def _number(value) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{round(number, 2):g}"


def _trim_label(values: dict) -> str:
    from editor.models import TRIM_PRESETS, StyleSheet

    trim = values.get("trim")
    if trim in TRIM_PRESETS:
        return TRIM_PRESETS[trim][0]
    width, height = values.get("width_mm"), values.get("height_mm")
    if width is not None and height is not None:
        return f"⁦{_number(width)}×{_number(height)}⁩ مم"
    return str(StyleSheet.Trim.CUSTOM.label)


def _label(path: str) -> str:
    if path in LABELS:
        return LABELS[path]
    from editor.models import StyleSheet

    try:
        return str(StyleSheet._meta.get_field(path.split(".")[0]).verbose_name)
    except Exception:  # noqa: BLE001 - a field the model does not name: its path
        return path


def _shown(path: str, value) -> str:
    """A value as the panel says it."""
    from editor.services import CHOICE_FIELDS
    from publishing.fonts import display_name

    if path in FACE_FIELDS:
        return display_name(value)
    if path in CHOICE_FIELDS:
        choices = CHOICE_FIELDS[path]
        try:
            return str(choices(value).label)
        except ValueError:
            return str(value)
    if isinstance(value, bool):
        return "نعم" if value else "لا"
    if isinstance(value, int | float):
        unit = UNITS.get(path)
        return f"{_number(value)} {unit}" if unit else _number(value)
    if value is None:
        return "تلقائي"
    return WORDS.get(str(value), str(value))


# ====================================================================== the changes a template brings


def applicable(values: dict, organization) -> tuple[dict, list[dict]]:
    """The values to apply to a book of `organization`: a face the organisation cannot offer now (removed,
    deleted, another organisation's) is left out and listed in `skipped` (`[{field, label, name,
    message}]`)."""
    from publishing.fonts import display_name, face_error

    out = {key: value for key, value in (values or {}).items()}
    skipped: list[dict] = []
    for name in FACE_FIELDS:
        if name not in out:
            continue
        message = face_error(out[name], organization, FACE_ROLES[name])
        if message:
            skipped.append(
                {"field": name, "label": LABELS[name], "name": display_name(out[name]), "message": message}
            )
            del out[name]
    return out, skipped


def template_changes(book, template: StyleTemplate) -> dict:
    """What applying `template` to `book` changes: `{changes: [{field, label, before, after}], skipped,
    same}` — one row per field (the trim with its size; the width and height of a preset trim are the
    trim's), in the order of the template's values; `same` when nothing changes."""
    from accounts.services import book_organization

    values, skipped = applicable(template.values, book_organization(book))
    before = book_values(book)
    after_flat = _flatten(values)
    before_flat = _flatten(before)
    changes: list[dict] = []
    trim_changed = values.get("trim") != before.get("trim") or (
        values.get("trim") == "custom"
        and (
            after_flat.get("width_mm") != before_flat.get("width_mm")
            or after_flat.get("height_mm") != before_flat.get("height_mm")
        )
    )
    for path, value in after_flat.items():
        if path in ("width_mm", "height_mm"):
            continue
        if path == "trim":
            if "trim" in values and trim_changed:
                changes.append(
                    {
                        "field": "trim",
                        "label": LABELS["trim"],
                        "before": _trim_label(before),
                        "after": _trim_label(values),
                    }
                )
            continue
        old = before_flat.get(path)
        if old == value or (
            isinstance(old, int | float) and isinstance(value, int | float) and float(old) == float(value)
        ):
            continue
        changes.append(
            {"field": path, "label": _label(path), "before": _shown(path, old), "after": _shown(path, value)}
        )
    return {"changes": changes, "skipped": skipped, "same": not changes}


def summary(template: StyleTemplate) -> list[str]:
    """A template in a line or two: trim, faces, body size and leading (the lists show it)."""
    from publishing.fonts import display_name

    values = template.values or {}
    parts = [_trim_label(values)]
    faces = [display_name(values[name]) for name in ("body_font", "heading_font") if values.get(name)]
    if faces:
        parts.append(" / ".join(dict.fromkeys(faces)))
    if values.get("body_size_pt") is not None:
        parts.append(f"{_number(values['body_size_pt'])} نقطة")
    if values.get("line_height") is not None:
        parts.append(f"تباعد {_number(values['line_height'])}")
    return parts


# ====================================================================== create, update, rename, delete, apply


def _clean_name(name, description) -> tuple[str, str]:
    name = " ".join(str(name or "").split())
    description = " ".join(str(description or "").split())
    if not name:
        raise TemplateError(NO_NAME)
    if len(name) > NAME_MAX:
        raise TemplateError(NAME_TOO_LONG)
    if len(description) > DESCRIPTION_MAX:
        raise TemplateError(DESCRIPTION_TOO_LONG)
    return name, description


def _check_book(organization, book) -> None:
    from accounts.services import book_organization

    owner = book_organization(book)
    if owner is None:
        raise TemplateError(NO_ORGANIZATION)
    if owner.pk != organization.pk:
        raise TemplateError(OTHER_ORGANIZATION)


def create_template(organization, book, name, description="", user=None) -> StyleTemplate:
    """A new template of `organization` from `book`'s current stylesheet. Raises `TemplateError`."""
    name, description = _clean_name(name, description)
    _check_book(organization, book)
    if StyleTemplate.objects.filter(organization=organization, name=name).exists():
        raise TemplateError(NAME_TAKEN)
    who = user if getattr(user, "is_authenticated", False) else None
    try:
        with transaction.atomic():
            return StyleTemplate.objects.create(
                organization=organization,
                name=name,
                description=description,
                values=book_values(book),
                source_book=book,
                created_by=who,
                updated_by=who,
            )
    except IntegrityError:
        raise TemplateError(NAME_TAKEN) from None


def update_from_book(template: StyleTemplate, book, user=None) -> StyleTemplate:
    """Take `book`'s current stylesheet into the template (its name and description stay)."""
    _check_book(template.organization, book)
    template.values = book_values(book)
    template.source_book = book
    template.updated_by = user if getattr(user, "is_authenticated", False) else None
    template.save(update_fields=["values", "source_book", "updated_by", "updated_at"])
    return template


def rename_template(template: StyleTemplate, name, description=None) -> StyleTemplate:
    """Change the template's name (and description when given)."""
    name, description = _clean_name(name, template.description if description is None else description)
    clash = StyleTemplate.objects.filter(organization_id=template.organization_id, name=name)
    if clash.exclude(pk=template.pk).exists():
        raise TemplateError(NAME_TAKEN)
    template.name = name
    template.description = description
    template.save(update_fields=["name", "description", "updated_at"])
    return template


def delete_template(template: StyleTemplate) -> None:
    """Delete the template (the books it was applied to keep their stylesheets)."""
    template.delete()


def apply_template(book, template: StyleTemplate, user=None) -> dict:
    """Apply `template` to `book` through the stylesheet's own validation and save: `{changed, skipped,
    stylesheet}`. Raises `TemplateError` (another organisation's book) or `editor.services
    .StyleSheetError`."""
    from accounts.services import book_organization
    from editor.services import update_stylesheet

    _check_book(template.organization, book)

    values, skipped = applicable(template.values, book_organization(book))
    sheet, changed = update_stylesheet(book, values, user)
    return {"changed": changed, "skipped": skipped, "stylesheet": sheet}
