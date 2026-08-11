# Plano de Desenvolvimento — Sistema de Biblioteca Comunitária

## 1. Modelagem de Dados

| Tabela | Campo | Tipo | Observações |
|---|---|---|---|
| **User** | id | string (cuid) | PK |
| | fullName | string | Nome completo |
| | email | string | único |
| | phone | string? | Telefone/WhatsApp |
| | document | string? | CPF, único |
| | passwordHash | string | gerado pelo provider de auth |
| | role | enum `ADMIN` \| `LEITOR` | default `LEITOR` |
| | createdAt | datetime | |
| **Book** | id | string (cuid) | PK |
| | code | string | único, gerado automaticamente (ver seção 2) |
| | title | string | |
| | author | string | nome completo do autor |
| | category | string? | |
| | status | enum `DISPONIVEL` \| `EMPRESTADO` \| `EM_MANUTENCAO` | default `DISPONIVEL` |
| | createdAt | datetime | |
| **Loan** | id | string (cuid) | PK |
| | bookId | string | FK -> Book.id |
| | userId | string | FK -> User.id (leitor) |
| | loanDate | datetime | default now() |
| | dueDate | datetime? | previsão de devolução (opcional) |
| | returnDate | datetime? | preenchido na devolução |
| | status | enum `ATIVO` \| `DEVOLVIDO` | default `ATIVO` |

Relacionamentos: `User 1:N Loan`, `Book 1:N Loan`. Regra de negócio (aplicada no backend, não no schema): um livro só pode ter **um** `Loan` com status `ATIVO` por vez — é isso que também controla o campo `Book.status`.

Schema Prisma equivalente:

```prisma
enum Role { ADMIN LEITOR }
enum BookStatus { DISPONIVEL EMPRESTADO EM_MANUTENCAO }
enum LoanStatus { ATIVO DEVOLVIDO }

model User {
  id           String   @id @default(cuid())
  fullName     String
  email        String   @unique
  phone        String?
  document     String?  @unique
  passwordHash String
  role         Role     @default(LEITOR)
  createdAt    DateTime @default(now())
  loans        Loan[]
}

model Book {
  id        String     @id @default(cuid())
  code      String     @unique
  title     String
  author    String
  category  String?
  status    BookStatus @default(DISPONIVEL)
  createdAt DateTime   @default(now())
  loans     Loan[]
}

model Loan {
  id         String     @id @default(cuid())
  bookId     String
  userId     String
  loanDate   DateTime   @default(now())
  dueDate    DateTime?
  returnDate DateTime?
  status     LoanStatus @default(ATIVO)
  book       Book       @relation(fields: [bookId], references: [id])
  user       User       @relation(fields: [userId], references: [id])
}
```

---

## 2. Lógica do Código do Livro

Entregue em `bookCode.ts` (+ `bookCode.test.ts` com 13 testes, todos passando). Regra implementada:

```
[3 primeiras letras do último token do nome do autor, maiúsculas]
+ [1ª letra do primeiro nome, maiúscula]
- [sequencial de 3 dígitos para aquele autor]
```

**Nota de design importante:** a especificação original tem uma ambiguidade — o exemplo dado ("João Mellão Neto" → `NETJ-001`) usa "Neto" (o *último token* do nome) como base do código, não "Mellão" como o texto da especificação sugeria. A função implementada replica exatamente esse exemplo por padrão (usa sempre o último token, mesmo que seja um sufixo geracional como Neto/Filho/Júnior/Sobrinho), mas expõe uma opção `treatSuffixAsSurname: false` para quem preferir usar o sobrenome "de verdade" antes do sufixo. Os dois comportamentos estão testados.

Casos de borda cobertos: autor com nome único, partículas (de/da/dos), sobrenomes com acento, sobrenomes com menos de 3 letras (completa com `X`), sobrenomes compostos com hífen, sequência além de 999, nome vazio e contagem inválida.

A mesma lógica foi **portada 1:1 para Python** dentro do `app.py` (protótipo) — validei as duas implementações lado a lado com os mesmos 13 casos e ambas retornam resultados idênticos, então o comportamento fica consistente entre o protótipo e a versão de produção.

---

## 3. Arquitetura de Pastas (Next.js App Router)

```
biblioteca-comunitaria/
├── prisma/
│   ├── schema.prisma
│   └── seed.ts                  # cria admin padrão + livros de exemplo
├── src/
│   ├── app/
│   │   ├── (auth)/
│   │   │   ├── login/page.tsx
│   │   │   └── cadastro/page.tsx
│   │   ├── (leitor)/
│   │   │   ├── catalogo/page.tsx
│   │   │   └── meus-emprestimos/page.tsx
│   │   ├── (admin)/
│   │   │   ├── livros/page.tsx
│   │   │   ├── emprestimos/page.tsx
│   │   │   └── importar/page.tsx
│   │   ├── api/
│   │   │   ├── auth/[...nextauth]/route.ts
│   │   │   ├── books/route.ts
│   │   │   ├── books/[id]/route.ts
│   │   │   └── loans/route.ts
│   │   ├── layout.tsx
│   │   └── page.tsx
│   ├── lib/
│   │   ├── prisma.ts
│   │   ├── auth.ts
│   │   └── bookCode.ts          # o arquivo já entregue e testado
│   ├── components/
│   │   ├── BookCard.tsx
│   │   └── LoanTable.tsx
│   └── middleware.ts             # protege /admin/* e /leitor/* por role
├── tests/
│   └── bookCode.test.ts
├── .env.local
├── package.json
└── tailwind.config.ts
```

---

## 4. Guia Passo a Passo

### 4.1 Preparar o projeto (comandos de terminal, dentro do WSL Ubuntu)

```bash
# fora do /mnt/c — dentro do filesystem nativo do Linux
cd ~ && mkdir -p projetos && cd projetos

# Node via nvm, se ainda não tiver Node instalado no WSL
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash
source ~/.bashrc
nvm install --lts

# projeto Next.js
npx create-next-app@latest biblioteca-comunitaria --typescript --tailwind --app --src-dir --eslint
cd biblioteca-comunitaria

# Prisma + SQLite local (dá para trocar por Postgres depois, sem reescrever a lógica)
npm install prisma @prisma/client
npx prisma init --datasource-provider sqlite

# Auth (RBAC) com NextAuth
npm install next-auth @next-auth/prisma-adapter bcryptjs
npm install -D @types/bcryptjs

# testes
npm install -D jest @types/jest ts-jest
npx ts-jest config:init

# abrir o Claude Code dentro da pasta do projeto
claude
```

### 4.2 Prompts para o Claude Code, fase a fase

Sugiro manter o mesmo estilo de fases curtas e objetivas que você já usa nos seus outros projetos — cada fase termina em algo testável antes de seguir pra próxima.

**Fase 1 — Schema e banco**
> Configure o schema.prisma com os models User, Book e Loan (enums Role, BookStatus, LoanStatus) conforme esta modelagem: [colar a seção 1 deste documento]. Rode a migration inicial e crie um seed.ts que cria um usuário admin padrão e 3-4 livros de exemplo.

**Fase 2 — Autenticação e RBAC**
> Configure o NextAuth com Credentials Provider e bcrypt para hash de senha. Crie o middleware.ts: usuário não logado → /login; leitor tentando acessar /admin/* → bloqueado; admin tem acesso total. Crie as páginas /login e /cadastro com o formulário completo (nome, e-mail, telefone/WhatsApp, CPF, senha).

**Fase 3 — Função de código do livro**
> Copie o arquivo bookCode.ts (função generateBookCode, já testada) para src/lib/bookCode.ts e o bookCode.test.ts para tests/. Rode os testes com npx jest e confirme que todos passam.

**Fase 4 — CRUD de livros (admin)**
> Crie as rotas em src/app/api/books/ (GET com filtro por título/autor/código/categoria, POST que chama generateBookCode com a contagem atual de livros do autor, PATCH, DELETE) e a página /admin/livros com formulário de cadastro e lista editável.

**Fase 5 — Catálogo público**
> Crie a página /catalogo com busca por título, autor, código e categoria, mostrando o status de cada livro com badges coloridos (verde=Disponível, vermelho=Emprestado, amarelo=Em Manutenção).

**Fase 6 — Empréstimo e devolução**
> Crie POST /api/loans (leitor solicita → livro vira Emprestado) e PATCH /api/loans/[id] (registra devolução → livro volta a Disponível). Crie /leitor/meus-emprestimos com histórico completo e livros em posse.

**Fase 7 — Painel de empréstimos (admin)**
> Crie /admin/emprestimos listando todos os empréstimos ativos, com botão para o admin registrar a devolução manualmente.

**Fase 8 — Importação inicial**
> Crie /admin/importar: upload de CSV (colunas titulo,autor,categoria), preview das linhas e confirmação, reaproveitando a mesma lógica de criação de livro (com generateBookCode) em lote.

**Fase 9 — Polimento e deploy**
> Revise responsividade mobile-first, adicione loading states e mensagens de erro/sucesso. Prepare o projeto para deploy na Vercel, trocando o datasource do Prisma para Postgres (Neon ou Supabase) via variável de ambiente.

---

## Arquivos já entregues nesta conversa

- `bookCode.ts` — função `generateBookCode` tipada, pronta para colar em `src/lib/`
- `bookCode.test.ts` — 13 testes Jest cobrindo os casos de borda
- `app.py` — protótipo funcional completo (Streamlit + SQLite), rodável agora com `streamlit run app.py`
