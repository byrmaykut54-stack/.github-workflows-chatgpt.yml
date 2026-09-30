# 🤖 AI İşletme Asistanı

Küçük işletmeler için Gemini destekli müşteri iletişim asistanı.

## MVP özellikleri

- Müşteri mesajını Gemini'ye gönderir.
- İşletmenin hizmet, fiyat ve çalışma bilgilerini bağlama ekler.
- Türkçe, kısa ve profesyonel cevap üretir.
- Bilinmeyen bilgileri uydurmaması için sistem talimatı kullanır.
- Basit HTTP API sunar.

## Kurulum

Python 3.10+ gerekir.

Gemini API anahtarını işletim sistemi ortam değişkeni olarak tanımlayın:

```text
GEMINI_API_KEY=...
```

API anahtarını kodun içine yazmayın.

Çalıştırma:

```bash
python business_assistant.py
```

Sağlık kontrolü:

```bash
curl http://localhost:8080/health
```

Mesaj gönderme:

```bash
curl -X POST http://localhost:8080/chat ^
  -H "Content-Type: application/json" ^
  -d "{\"message\":\"Yarın saat 18:00'de saç ve sakal için yer var mı?\"}"
```

## İşletmeyi değiştirme

`business_config.json` içindeki işletme adı, sektör, hizmetler, fiyatlar, çalışma saatleri ve adresi değiştirin.

## Güvenlik

Gemini anahtarı GitHub'a commit edilmemelidir. GitHub Actions gibi ortamlarda secret kullanın.

## Sonraki sürüm

- WhatsApp entegrasyonu
- Instagram DM entegrasyonu
- Google Calendar randevu kontrolü
- Müşteri kayıt sistemi
- Otomatik hatırlatma
- Günlük işletme raporu
- Web paneli
- Ücretli Pro plan
