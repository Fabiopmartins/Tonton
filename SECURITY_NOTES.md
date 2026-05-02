# Endurecimento aplicado

## O que foi alterado

1. **Senhas**
   - Hash com `scrypt` via Werkzeug.
   - Suporte a **pepper** separado por variável `PASSWORD_PEPPER`.
   - Rehash automático no login para hashes antigos.
   - Não há mais senha fixa insegura no bootstrap do admin.

2. **Usuário / LGPD**
   - O campo `users.email` passa a funcionar como **índice cego** (`HMAC-SHA256`) para busca.
   - O e-mail legível fica em `users.email_encrypted`.
   - O nome legível fica em `users.display_name_encrypted`.
   - Isso reduz exposição direta de PII em caso de leitura indevida do banco.

3. **Sessão e transporte**
   - `SESSION_COOKIE_HTTPONLY=True`
   - `SESSION_COOKIE_SECURE=True` quando `PUBLIC_BASE_URL` usa `https://`
   - `Strict-Transport-Security` em origem HTTPS.
   - `ProxyFix` para operação atrás de proxy no Railway.

## Observações importantes

- **Senha não deve ser criptografada reversivelmente**. O correto é **hash**, conforme OWASP e documentação do Werkzeug.
- **PII reversível** (e-mail, nome) foi protegida com `Fernet`, porque o sistema precisa ler esses dados para login/OAuth e envio de e-mail.
- Os segredos devem ficar **somente nas variáveis do Railway**, nunca no repositório.
- Se você trocar `PASSWORD_PEPPER`, os usuários precisarão redefinir a senha.
- Se você perder `PII_ENCRYPTION_KEY`, não conseguirá mais descriptografar os campos protegidos.

## Recomendação de produção

- Configure no Railway:
  - `FLASK_SECRET_KEY`
  - `PASSWORD_PEPPER`
  - `USER_LOOKUP_PEPPER`
  - `PII_ENCRYPTION_KEY`
  - `CODE_ENCRYPTION_KEY`
- Use valores longos e aleatórios.
- Faça rotação controlada de segredos.
- Restrinja o acesso ao banco e ao painel do Railway.
- Não exponha dumps do banco fora de storage seguro.

## Limitação atual

Este pacote endurece **credenciais de usuário** e PII do módulo de usuários. Os dados de gift cards continuam majoritariamente em texto no banco atual. Para LGPD mais completa, o próximo passo é aplicar o mesmo padrão a:
- `recipient_email`
- `recipient_phone`
- `buyer_phone`
- eventuais dados de clientes/CRM futuros
