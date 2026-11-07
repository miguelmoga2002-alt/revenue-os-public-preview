# Revenue OS — menos administración, más tiempo para vender

**AI Sales Operating System en construcción** · CRM B2B y workspace comercial · **Public Technical Preview**

Revenue OS persigue una idea: que cada comercial sepa **qué oportunidad atender, por qué y qué acción registrar a continuación**. El objetivo es reducir trabajo manual y mejorar la trazabilidad de las decisiones comerciales; **el ahorro de tiempo y las mejoras de ventas son hipótesis pendientes de medición**, no resultados ya obtenidos.

> **Estado:** versión candidata de laboratorio (`1.1.0-rc.1`), probada con datos sintéticos. **No lista para piloto con datos reales ni producción.** Los modelos de lenguaje, la transcripción de llamadas y los agentes no están integrados en el backend actual.

## Producto: qué funciona

| Componente | Estado |
| --- | --- |
| **Pipeline / Kanban** | Implementado: oportunidades, etapas, cambios de estado y permisos |
| **Deal Workspace / Account 360** | Implementado: relaciones, notas persistentes, timeline y referencias |
| **Focus / Today** | Implementado: cola de tareas y siguiente acción **determinista, explicable** |
| **Gestión comercial** | Implementado: asignaciones manager→comercial, reprogramación, resultados e idempotencia |
| **Migration Studio** | Implementado: CSV/XLSX, preview, mapeo, relaciones y rollback condicionado |
| **Seguridad multiempresa** | Implementado: roles, acceso por registro, MFA TOTP, sesiones revocables, PostgreSQL RLS, auditoría |
| **Enlaces de oportunidades** | Implementado: `/deals/{uuid}` y `/focus?record={uuid}` sujetos a ACL |
| **CRM OAuth (Pipedrive / HubSpot / Salesforce)** | **Experimental**, deshabilitado y probado con respuestas simuladas; sin conexión real validada |
| **OpenAI/Claude, call intelligence, email/calendar, Slack, agentes y envíos** | **Roadmap**: no implementado ni conectado en esta vista previa |

La priorización actual procede de reglas sobre datos y estados persistidos. **No se utilizan LLMs para tomar decisiones en esta release.** La IA generativa futura requerirá trazabilidad de fuentes, evaluación, consentimiento y aprobación humana antes de comunicaciones externas.

## Stack técnico verificado

### Backend
- **Python 3.12**, **FastAPI**, **Uvicorn**, **Pydantic v2** (API y validación).
- **SQLAlchemy 2**, **Alembic**, **psycopg 3** (persistencia, transacciones y migraciones).
- **httpx** (HTTP de conectores experimentales), **python-multipart** (uploads), **cryptography/Fernet** (protección de secretos).
- PBKDF2-HMAC-SHA256, TOTP RFC 6238 y parser CSV/XLSX propio mediante librería estándar.

### Frontend
- **HTML5, CSS3 y JavaScript nativo**, Fetch API, History API y Clipboard API.
- Pantallas `web/index.html` (Migration Studio), `web/crm.html` (pipeline y Account 360), `web/focus.html` (trabajo comercial).
- **No React, Next.js, Angular, Vue, Tailwind ni TypeScript** en el código publicado.

### Datos, seguridad e infraestructura
- **PostgreSQL 16** con RLS por tenant, rol restringido `revenue_app`; **SQLite** para desarrollo/tests.
- **Docker**, **Docker Compose** (servicios `postgres`, `migrate`, `app`), Python 3.12 Slim y PostgreSQL Alpine.
- Contenedor API sin privilegios, filesystem de solo lectura, capacidades retiradas y `no-new-privileges`.
- Ejemplo de **Caddy** para reverse proxy HTTPS; **no se instala ni despliega automáticamente**.

### Testing, desarrollo y entrega
- **Git, GitHub Actions, pytest, Playwright/Chromium, axe-core, Node.js 22**, Bandit y pip-audit en el desarrollo.
- `requirements.txt` / `requirements-dev.txt`, `.env.example`, `.gitignore`, scripts de PowerShell y Shell.
- **OpenAI SDK, Anthropic SDK, LangChain, Ollama, PyTorch y pgvector no figuran entre las dependencias de esta versión.**

## Arquitectura

```text
Navegador (HTML/CSS/JavaScript)
          │ fetch() / sesión autenticada
          ▼
FastAPI / Pydantic ── autorización tenant, role y grants
          │ SQLAlchemy + psycopg
          ▼
PostgreSQL 16 / RLS / transacciones / auditoría
          ▲
Servicio separado de migración Alembic

Pipedrive, HubSpot, Salesforce: adaptadores experimentales inactivos
LLM, correo/calendario, Slack, telefonía: visión, NO activos
```

## Ejecución local con Docker Compose

**Requisitos:** Git + Docker Engine/Desktop con Compose; memoria y espacio suficientes para PostgreSQL. Ejecutar **solo con datos sintéticos y en un equipo de desarrollo**, nunca exponer este Compose directamente a Internet.

### 1. Clonar y preparar secretos locales

```bash
git clone https://github.com/miguelmoga2002-alt/revenue-os-public-preview.git
cd revenue-os-public-preview
cp .env.example .env
```

En PowerShell: `Copy-Item .env.example .env`.

**Antes de arrancar**, editar `.env` y sustituir `POSTGRES_PASSWORD`, `POSTGRES_APP_PASSWORD` por secretos aleatorios diferentes de **al menos 20 caracteres**. Generar una clave Fernet privada para `REVENUE_OS_MFA_ENCRYPTION_KEY`:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

La orden requiere `cryptography` en el entorno Python (`pip install cryptography` si no está instalado). **Nunca subir esa clave ni el archivo `.env` a GitHub**. Mantener `REVENUE_OS_REQUIRE_ADMIN_MFA=1`, `REVENUE_OS_ENV=development` y conectores desactivados.

### 2. Arrancar infraestructura (un comando tras configurar el entorno)

```bash
docker compose up -d --build app
```

Compose inicia PostgreSQL, aplica Alembic, aprovisiona el rol restringido y arranca FastAPI. Tras el arranque, comprobar disponibilidad:

```bash
docker compose ps
docker compose logs --tail 30 app
```

Si `/health` no responde de inmediato, verificar logs: Uvicorn puede tardar en iniciarse.

### 3. Crear un administrador ficticio y enrolar MFA (primera instalación)

```bash
docker compose run --rm migrate python bootstrap.py demo admin-demo
docker compose run --rm migrate python -m scripts.enroll_admin_mfa admin-demo
```

La contraseña se introduce de forma privada (mínimo 14 caracteres). La segunda orden genera una URI TOTP **secreta**, que se utiliza solo en el terminal de confianza y **nunca** se comparte por chats, capturas o tickets.

### 4. Abrir la app

| Página | URL local |
| --- | --- |
| CRM / Kanban | http://127.0.0.1:8000/crm |
| Focus comercial | http://127.0.0.1:8000/focus |
| Migration Studio | http://127.0.0.1:8000/ |
| OpenAPI | http://127.0.0.1:8000/docs |
| Estado | http://127.0.0.1:8000/health |

Por seguridad, Docker expone `8000` y `5432` **solo en 127.0.0.1**. **Este procedimiento no crea una instalación pública ni un SaaS productivo.**

### Probar el backend en aislamiento

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q tests
```

Las pruebas PostgreSQL/E2E requieren servicios y variables de test adicionales; consultar `.github/workflows/ci.yml`. No ejecutar pruebas de migración o reversión contra datos reales. **No utilizar `docker compose down -v` si deseas conservar la base de datos local.**

## Seguridad y limitaciones de esta preview

- Backend restringe el acceso por tenant y grants; conocer una URL no concede acceso.
- El logout revoca la sesión del servidor en el flujo correcto; su última aceptación manual local y el proceso de release del proyecto principal son asuntos independientes de este repositorio público.
- No contiene datos de clientes, API keys, credenciales válidas ni base de datos. El archivo `.env.example` contiene solo nombres y valores de ejemplo.
- Las pruebas sintéticas y el hardening implementado **no equivalen a un pentest, certificación WCAG, cumplimiento RGPD/AI Act ni SLA**.
- El backend actual **no** envía emails, graba llamadas ni consulta LLMs de OpenAI/Anthropic.
- El código es una vista previa técnica, **no una invitación a integrar datos personales ni un compromiso de soporte**.

## Roadmap hacia un AI Sales OS

1. Validar ICP, entrevistas de descubrimiento y cuantificar minutos de administración por oportunidad.
2. Integraciones reales, consentidas y auditables (un CRM, correo/calendario y Slack), aprobadas en sandbox.
3. Extracción de señales con **fuente, timestamp, confianza y corrección humana**.
4. Recomendaciones y borradores con pruebas de calidad, coste y **aprobación antes de enviar**.
5. Observabilidad, backups operativos y controles legales antes de cualquier piloto con clientes.

## Empresas: buscamos feedback

¿Lideras un equipo B2B de **10 a 50 comerciales en España**? Nos interesa entender cómo gestionáis los seguimientos, cuánto tiempo se invierte en actualizar el CRM y qué información necesita un manager para confiar en su pipeline.

**Convocatoria:** entrevistas de producto y demos con datos ficticios. Para solicitar conversación, abre un [issue de interés en Revenue OS](https://github.com/miguelmoga2002-alt/revenue-os-public-preview/issues/new) con el título `Design partner — interés`, **sin datos personales de clientes ni credenciales**.

## Licencia y contribuciones

**Licencia no concedida por ahora.** La visibilidad pública del código no significa que se autorice su redistribución o uso comercial. Se decidirá una licencia explícita cuando esté acordada la estrategia open source/comercial.

Las PRs y las cuestiones de seguridad deben tratarse conforme a `SECURITY.md`. Este repositorio usa **historial Git nuevo**, sin el historial privado del desarrollo original.
