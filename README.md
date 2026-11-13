# TriAevum-OoT3DS-Arabic

محول تعريب الحلم المتجدد للعبة **Ocarina of Time 3D (OoT3DS)** إلى نسخة الحاسب **TriAevum — Echoes of Time**.

> **ملاحظة:** هذا المستودع يحتوي على أداة التثبيت فقط (`build_custom_o3ro.exe` و `تثبيت تعريب الحلم المتجدد.bat`)، ولا يحتوي على أي ملفات لعبة أو ملفات تعريب.

---

## طريقة التثبيت والتشغيل

1. **تنزيل اللعبة:** حمّل أحدث إصدار من مشروع **TriAevum** عبر الرابط الرسمي:  
   https://github.com/coccofresco/TriAevum
2. **تجهيز اللعبة:** فك الضغط، ثم شغّل **`TriAevumForge.exe`** واختر ملف الـ ROM الخاص بك (نسخة EUR بصيغة `.3ds` أو `.cci`)، ثم اضغط **Prepare** وانتظر حتى ينتهي البناء.
3. **إضافة أداة التعريب:** حمّل الأداة من صفحة **[Releases](https://github.com/ALJ518/TriAevum-OoT3DS-Arabic/releases)** في هذا المستودع، وضع ملفي **`build_custom_o3ro.exe`** و **`تثبيت تعريب الحلم المتجدد.bat`** داخل مجلد اللعبة الرئيسي (بجانب `Echoes_of_Time.exe`).
4. **تنزيل التعريب:** حمّل ملف التعريب المضغوط (`.zip`) من صفحة فريق الحلم المتجدد:  
   https://etrdream.com/game/zelda-ocarina-of-time/
5. **التثبيت:** اسحب ملف التعريب الـ **ZIP** وأفلته مباشرةً فوق ملف **`تثبيت تعريب الحلم المتجدد.bat`** (لا يحتاج بايثون أو أي برامج إضافية).
6. **التشغيل:** بعد انتهاء التثبيت، افتح **`Echoes_of_Time.exe`** مباشرةً وستعمل اللعبة بالتعريب.

---

## للمطورين (البناء من السورس)
* السكربت المصدري موجود في `build_custom_o3ro.py`.
* للتحويل إلى `.exe` استخدم:
  ```powershell
  pip install pyinstaller pillow
  pyinstaller --onefile --console --clean --name build_custom_o3ro build_custom_o3ro.py
