# 🚂 Deploy da Tonton no Railway — passo a passo

Este guia leva a aplicação **do zero ao ar** no Railway, com Postgres provisionado, admin inicial criado e a Laís logando no painel da Tonton.

> Tempo estimado: **15–20 minutos** se você já tem conta no GitHub e Railway.

---

## 🔑 ACESSO INICIAL — o que você vai usar pra logar

Quando o app subir pela primeira vez, **um admin é criado automaticamente** no banco. Você pode controlar essas credenciais com duas variáveis de ambiente (passos 4 e 5 abaixo):

- **`ADMIN_USERNAME`** → vira o e-mail de login
- **`ADMIN_PASSWORD_HASH`** → o hash da senha

Se você **não definir** essas variáveis, o app cria um admin com **defaults inseguros**:

| Campo | Default |
|---|---|
| **E-mail** | `admin@tonton.local` |
| **Senha** | `Troque-esta-senha` |

⚠️ **Esses defaults só servem pra primeiro acesso emergencial. Configure as vars antes do primeiro boot — o passo 5 mostra como.**

---

## 1. Pré-requisitos

- Conta no [GitHub](https://github.com) com o código deste projeto num repositório (público ou privado, tanto faz).
- Conta no [Railway](https://railway.com) — pode usar login via GitHub.
- Cartão de crédito cadastrado no Railway (mesmo no plano Hobby).

### Subindo o código no GitHub

Se ainda não está num repo:

```bash
# dentro da pasta Tonton_cupom-main
git init
git add .
git commit -m "feat: painel Tonton (rebrand Malê)"
git branch -M main
git remote add origin https://github.com/SEU-USER/tonton-painel.git
git push -u origin main
```

---

## 2. Criar o projeto no Railway

1. Vá em [railway.com/new](https://railway.com/new).
2. Clique **"Deploy from GitHub repo"**.
3. Autorize o Railway a ver seus repositórios e selecione o repo `tonton-painel`.
4. O Railway detecta que é uma app Python (pelo `requirements.txt` + `Procfile`), começa o build com **Nixpacks** e tenta subir um serviço.

> Nesse primeiro boot o app **vai falhar** com erro `DATABASE_URL não configurada`. Isso é esperado — o passo 3 resolve.

---

## 3. Adicionar o Postgres

No canvas do projeto (a tela com os "tijolos" dos serviços):

1. Clique **"+ Create"** no canto superior direito.
2. Escolha **"Database" → "Add PostgreSQL"**.
3. O Railway provisiona o Postgres em ~30 segundos. Vai aparecer um segundo tijolo no canvas.

O Postgres do Railway expõe automaticamente as variáveis `PGHOST`, `PGPORT`, `PGUSER`, `PGPASSWORD`, `PGDATABASE` e — a que importa pra gente — **`DATABASE_URL`**.

---

## 4. Conectar o app ao Postgres

1. Clique no tijolo da **app** (não no do Postgres).
2. Vá na aba **"Variables"**.
3. Clique **"+ New Variable"** → **"Add Reference"**.
4. Selecione `Postgres.DATABASE_URL`. Vai aparecer assim:

   ```
   DATABASE_URL = ${{Postgres.DATABASE_URL}}
   ```

   Esse `${{ ... }}` é uma referência viva — se a senha do Postgres mudar, o app pega a nova automaticamente.

---

## 5. Configurar as variáveis de ambiente

Ainda na aba **Variables** do serviço da app, adicione **uma a uma** clicando em **"+ New Variable"**. 🔴 = obrigatórias, 🟡 = recomendadas, 🟢 = opcionais.

### 🔴 Obrigatórias

| Variável | Valor | O que é |
|---|---|---|
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` | (já feito no passo 4) |
| `FLASK_SECRET_KEY` | gerar abaixo | Chave que protege os cookies de sessão. **Sem ela, sessões não sobrevivem restart e múltiplos workers brigam.** |
| `ADMIN_USERNAME` | seu e-mail real | E-mail do primeiro admin. Ex: `lais@tontonlojainfantil.com.br` |
| `ADMIN_PASSWORD_HASH` | hash da senha (ver abaixo) | Hash da senha do admin (não a senha em texto puro!) |

#### Como gerar `FLASK_SECRET_KEY`

No seu computador, com Python instalado:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Saída tipo: `a8f3d92e5c1b...` (64 caracteres hex). Copia tudo e cola no campo "value" da variável.

#### Como gerar `ADMIN_PASSWORD_HASH`

A senha precisa ir como **hash**, não em texto puro. No seu computador:

```bash
pip install werkzeug
python3 -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('SUA_SENHA_AQUI', method='scrypt'))"
```

Troque `SUA_SENHA_AQUI` por uma senha forte (anota num gerenciador de senhas!). A saída é tipo:

```
scrypt:32768:8:1$lIOg...$a3f4b2c1...
```

Copia **a string toda** (do `scrypt:` até o final) e cola na variável `ADMIN_PASSWORD_HASH`.

> 💡 **Atalho preguiçoso**: se você não quiser gerar hash agora, **não defina** `ADMIN_PASSWORD_HASH`. O app cria com a senha default `Troque-esta-senha`. Logue, vá direto em **Configurações → Usuários** e mude pra uma senha forte. Depois, defina a variável corretamente pra blindar futuros redeploys.

### 🟡 Recomendadas (segurança extra)

| Variável | Como gerar | Pra quê |
|---|---|---|
| `PASSWORD_PEPPER` | `python3 -c "import secrets; print(secrets.token_hex(32))"` | "Tempero" extra no hash de senhas — protege se o banco vazar |
| `USER_LOOKUP_PEPPER` | mesmo comando | Protege a busca de usuários por e-mail |
| `PII_ENCRYPTION_KEY` | `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` | Criptografa dados sensíveis de cliente (CPF, telefone) no banco |
| `CODE_ENCRYPTION_KEY` | mesmo comando do anterior | Criptografa códigos de vale-presente em repouso |
| `CODE_PEPPER` | `python3 -c "import secrets; print(secrets.token_hex(32))"` | Tempero do hash dos códigos de vale-presente |
| `SESSION_COOKIE_SECURE` | `true` | Força cookies só por HTTPS (Railway é HTTPS por padrão) |

### 🟢 Opcionais (configure quando precisar)

| Variável | Pra quê |
|---|---|
| `MAIL_FROM` | E-mail remetente das mensagens transacionais (ex: `lais@tontonlojainfantil.com.br`) |
| `MAIL_FROM_NAME` | Nome do remetente (default já é `Tonton`) |
| `BREVO_API_KEY` | Chave da Brevo (ex-Sendinblue) pra enviar e-mails de vale-presente. Sem isso, os e-mails não saem mas o painel funciona normal. |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD` | Alternativa à Brevo: SMTP genérico (Gmail, Zoho, Mailgun, etc.) |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | Login com Google (opcional, pra atendentes) |
| `PUBLIC_BASE_URL` | URL pública da app, ex: `https://tonton-painel.up.railway.app`. Importante pra QR codes de vale-presente apontarem certo. |

> Cada variável adicionada **dispara um redeploy automático**. Pode adicionar várias e esperar o deploy final.

---

## 6. Configurar domínio público

1. No tijolo da app, vá na aba **"Settings"**.
2. Role até **"Networking"** → **"Public Networking"**.
3. Clique **"Generate Domain"**.
4. O Railway gera algo como `tonton-painel-production.up.railway.app`.
5. **Copie essa URL**, volte na aba **Variables**, edite (ou crie) `PUBLIC_BASE_URL` com esse valor (com `https://` na frente).

> Domínio próprio (ex: `painel.tontonlojainfantil.com.br`)? Em **Networking → Custom Domain**, segue o passo a passo do Railway de adicionar um CNAME no seu DNS. Pode ser feito depois.

---

## 7. Primeiro boot — o que esperar nos logs

Vá na aba **"Deployments"** → última build → **"View Logs"**. Você deve ver, em ordem:

```
[INFO] Starting gunicorn 23.0.0
[INFO] Listening at: http://0.0.0.0:8080
[INFO] Schema Postgres ausente. Aplicando schema_pg.sql automaticamente...
[INFO] Schema aplicado com sucesso.
[INFO] role_hmac bootstrap: 0 usuarios
```

Isso quer dizer que:
- ✅ App subiu
- ✅ Conectou no Postgres
- ✅ Criou todas as tabelas (gift_cards, products, customers, sales, ...)
- ✅ Criou o admin inicial com as credenciais que você definiu

**Erro comum**: `DATABASE_URL não configurada` → volte no passo 4.
**Outro comum**: `psycopg2.OperationalError` → o Postgres ainda está acordando, espera 30s e clica "Restart" no deployment.

---

## 8. Primeiro login 🎉

1. Abra `https://SEU-DOMINIO.up.railway.app/login` no navegador.
2. Faça login com:
   - **E-mail**: o valor que você pôs em `ADMIN_USERNAME` (ou `admin@tonton.local` se não definiu)
   - **Senha**: a que você usou pra gerar o `ADMIN_PASSWORD_HASH` (ou `Troque-esta-senha` se não definiu)
3. Você cai no **Dashboard** da Tonton.

### ⚠️ Logo após o primeiro login

1. Vá em **Configurações → Usuários** e troque a senha do admin pra uma forte (se usou o default).
2. Em **Configurações → Loja**, configure:
   - Nome legal e fantasia da loja (Tonton Loja Infantil)
   - CNPJ/MEI (se houver)
   - Endereço (Timóteo/MG)
   - Instagram: `@tontonlojainfantil`
   - WhatsApp da loja
3. Em **Configurações → Marketing**, ajuste os templates de mensagem do WhatsApp se quiser.
4. Cadastre os primeiros produtos em **Catálogo → Novo produto** com os tamanhos da Tonton (`2, 4, 6, 8, 10 anos`).

---

## 9. Backups do banco

O Railway faz **snapshots automáticos** dos serviços de banco. Você consegue restaurar via dashboard do Postgres → "Backups".

Pra um backup manual a qualquer momento (recomendado antes de mudanças grandes):

```bash
# instale o Railway CLI uma vez
npm i -g @railway/cli
railway login
railway link        # selecione seu projeto

# pega a DATABASE_URL e dumpa
railway run --service Postgres pg_dump $DATABASE_URL > backup-$(date +%Y%m%d).sql
```

---

## 10. Atualizando o código depois

Cada `git push` na branch `main` dispara um redeploy automático no Railway. Na primeira vez que rodar com schema mudado, o `init_db()` aplica as **migrations incrementais** automaticamente. Para mudanças destrutivas (drop column, rename), o app **trava o boot** com erro pedindo migration manual via `psql` — proteção pra não perder dados.

---

## 11. Solução de problemas

| Sintoma | Provável causa | O que fazer |
|---|---|---|
| Login dá "credenciais inválidas" sempre | `ADMIN_PASSWORD_HASH` foi colado com aspas, ou a senha não bate | Regere o hash, cole sem aspas. Em último caso, apague a variável (volta ao default `Troque-esta-senha`) e troque a senha pelo painel depois. |
| 500 ao abrir qualquer página | Schema corrompido ou parcial | Logs vão mostrar a coluna que falta. No painel do Railway, no Postgres, clique "Connect" → CLI e rode `\dt` pra ver as tabelas. Se vazio, force redeploy do app. |
| Cookies de sessão somem após F5 | `FLASK_SECRET_KEY` não definida e tem múltiplos workers | Defina a `FLASK_SECRET_KEY` (passo 5). |
| Catálogo público lento | Imagens muito grandes servidas pelo Postgres | Considere migrar uploads pra S3/Backblaze depois. Versão atual aguenta umas centenas de produtos sem problema. |
| Vale-presente não envia e-mail | Falta `BREVO_API_KEY` ou `SMTP_*` | O painel sinaliza. Configure uma das duas opções na aba Variables. |
| "Application failed to respond" no domínio | Worker travado | Aba Deployments → "Restart". Se voltar, ver logs. |

---

## 📚 Referências

- Documentação Railway: https://docs.railway.com
- Postgres no Railway: https://docs.railway.com/guides/postgresql
- Cron jobs (futuro, pra campanhas agendadas): https://docs.railway.com/reference/cron-jobs

---

**Pronto. Tonton no ar.** 🧡 Se travar em qualquer ponto, manda print do log que a gente decifra.
