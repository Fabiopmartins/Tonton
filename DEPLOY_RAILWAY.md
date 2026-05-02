# Implantação no Railway

## 1. Subir o código
- envie este projeto para um repositório GitHub
- no Railway: **New Project** -> **Deploy from GitHub Repo**

O Railway possui guia oficial para Flask e permite deploy por GitHub, CLI ou template. citeturn507426search11

## 2. Criar o PostgreSQL
No canvas do projeto:
- **Create** -> **Database** -> **Add PostgreSQL**

O serviço PostgreSQL do Railway expõe `PGHOST`, `PGPORT`, `PGUSER`, `PGPASSWORD`, `PGDATABASE` e `DATABASE_URL`. Muitas bibliotecas usam `DATABASE_URL` automaticamente. citeturn507426search0

## 3. Variáveis no serviço web
No serviço da aplicação, configure:
- `DATABASE_URL=${{Postgres.DATABASE_URL}}`
- `FLASK_SECRET_KEY=` valor aleatório forte
- `PASSWORD_PEPPER=` valor aleatório forte
- `USER_LOOKUP_PEPPER=` valor aleatório forte
- `PII_ENCRYPTION_KEY=` valor aleatório forte
- `ADMIN_EMAIL=` seu e-mail inicial
- `ADMIN_NAME=` seu nome
- `ADMIN_PASSWORD=` senha inicial forte
- `SESSION_COOKIE_SECURE=true`

As variables ficam disponíveis no build, no runtime e também via `railway run`/`railway shell`. citeturn507426search19

## 4. Start command
Use no Railway:
```bash
gunicorn app:app
```

Se Railway não detectar comando de start, ele deve ser definido manualmente nas configurações do serviço. citeturn507426search21

## 5. Persistência
Este projeto **não** depende de salvar dados em disco local. Os dados ficam no PostgreSQL.

Isso é importante porque volumes do Railway são montados apenas quando o container inicia, não no build nem no pre-deploy. citeturn507426search1

## 6. Primeiro acesso
Após o deploy:
- abra o domínio gerado pelo Railway
- faça login com `ADMIN_EMAIL` e `ADMIN_PASSWORD`
- troque a senha depois criando um segundo admin e desativando o padrão, se desejar

## 7. Cron jobs futuros
Para campanhas agendadas, aniversariantes e alertas automáticos, use um serviço separado ou o próprio serviço com cron configurado em **Settings -> Cron Schedule**. O Railway executa o serviço no agendamento definido por expressão crontab. citeturn507426search2turn507426search6

## 8. Observações técnicas
- este MVP usa `db.create_all()` para iniciar tabelas automaticamente
- como você informou que não há dados relevantes atuais, não foi criada rotina de migração
- para fase seguinte, o ideal é evoluir para `Flask-Migrate` e versionamento de schema
- a integração WhatsApp atual é por `wa.me`; disparo em massa deve migrar depois para API oficial do WhatsApp Business
