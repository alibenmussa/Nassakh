import re
from core.arabic import strip_tashkeel
RASM = str.maketrans({"ب": "ٮ", "ت": "ٮ", "ث": "ٮ", "ن": "ٮ", "ي": "ٮ", "ى": "ٮ", "ئ": "ٮ", "ج": "ح", "خ": "ح", "ذ": "د", "ز": "ر", "ش": "س",
                      "ض": "ص", "ظ": "ط", "غ": "ع", "ق": "ڡ", "ف": "ڡ", "ة": "ه", "ؤ": "و", "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ء": None, "ـ": None})
def rasm(w):
    w = strip_tashkeel(w or "")
    w = re.sub(r"[^ء-ي]", "", w)
    return w.translate(RASM)
