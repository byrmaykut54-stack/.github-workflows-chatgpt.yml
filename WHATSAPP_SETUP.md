# WhatsApp entegrasyonu

Bu proje WhatsApp Business Cloud API için webhook'a hazırdır.

## Webhook uçları

- GET /webhook/whatsapp — doğrulama
- POST /webhook/whatsapp — gelen mesaj

## Sonraki bağlantı

1. Meta for Developers'ta WhatsApp Business uygulaması oluşturulur.
2. Kalıcı erişim belirteci ve telefon numarası ID alınır.
3. Public HTTPS adresi bu webhook'a bağlanır.
4. Verify Token uygulama ortam değişkeni olarak tanımlanır.
5. Gelen WhatsApp mesajı /webhook/message akışına aktarılır.
6. AI cevabı WhatsApp Cloud API üzerinden müşteriye gönderilir.

## Güvenlik

Webhook doğrulama belirteci GitHub'a yazılmamalıdır. Secret/environment variable olarak tutulmalıdır.

Bu MVP gerçek WhatsApp mesajı göndermek için Meta kimlik bilgileri gerektirir.
