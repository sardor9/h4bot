# H4 Sweep · M15 CISD / Model-1 bot

1. Yangi GitHub repo oching, shu fayllarni yuklang.
2. Settings → Secrets and variables → Actions → New secret:
   - `TWELVEDATA_KEY` (twelvedata.com bepul kalit)
   - `TG_TOKEN` (@BotFather bergan token)
   - `TG_CHAT` (chat id: botga yozing, so'ng @userinfobot dan oling)
3. Settings → Pages → Source: `main` branch, `/docs` papka.
4. Actions → scan → Run workflow (sinash uchun). Keyin har 15 daqiqada o'zi ishlaydi.

Sozlamalar (workflow `env` ga qo'shing): `RR` (2), `BE_R` (1, 0=o'chiq), `MODES` (`cisd,model1`).
