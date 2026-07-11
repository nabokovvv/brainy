# Brainy Website: CMS, миграция и автономный контентный pipeline

Статус: ADR и execution plan, 2026-07-12.

## Outcome

Сайт Brainy развивается как отдельная от Telegram runtime система:

```text
topic discovery
  -> editorial scoring
  -> safe research + evidence bundle
  -> English draft
  -> deterministic and editorial QA
  -> automatic quality gates
  -> scheduled publish
  -> asynchronous translations
  -> locale QA + publish
```

Источник истины — Markdown и metadata в Git в новом **private** repository.
Целевой генератор сайта — Astro,
хостинг — Cloudflare static hosting. Для нового проекта предпочтительны Workers
Static Assets; если аудит Cloudflare account подтвердит, что текущий проект уже
работает через Pages, можно сохранить Pages. Отдельная database-backed CMS в первом
релизе не нужна. Если позже понадобится визуальное редактирование человеком, поверх
того же Git content model можно добавить Decap CMS, не меняя формат статей.

Pipeline не читает пользовательские разговоры Brainy, не работает внутри процесса
Telegram-бота и не делит с ним необозначенную внешнюю квоту. Он может переиспользовать
provider-neutral inference/search contracts и безопасный page fetcher Brainy как
библиотечные компоненты.

### Repository boundary

- публичный `brainy` остаётся кодом Telegram-бота и публичной policy/documentation;
- публичный `AskBrainyWebsite` остаётся immutable legacy archive и rollback source;
- создаётся новый private GitHub repository `askbrainy-publisher` (название можно
  поменять до создания): Astro source, Markdown content, pipeline code, prompts,
  job ledger schema, provider configuration and deployment infrastructure живут
  только там;
- Cloudflare получает доступ к private repo для build/deploy, но опубликованный
  static site остаётся публичным;
- общие безопасные contracts можно позже вынести в public package, но private
  publisher никогда не зависит от публикации prompts или editorial workflow.

## Context

Текущий `AskBrainyWebsite` — опубликованный результат Pelican, а не исходный проект:

- в Git сохранены сгенерированные HTML, CSS и изображения;
- сохранено 10 статей в 8 локалях: `en`, `de`, `es`, `fr`, `id`, `pt`, `ru`, `tr`;
- английские статьи опубликованы в корне, переводы — под `/{locale}/`;
- HTML содержит тексты, даты, категории, теги, источники, изображения, canonical,
  reciprocal `hreflang`, `x-default` и author block;
- текущий sitemap содержит 104 URL: 8 homepages и 96 article/legal pages;
- старые Pelican sources/config/theme в репозитории отсутствуют.

Следовательно, существующий контент не потерян, но миграция должна извлечь
структурированные документы из HTML. Перенос вручную не нужен.

Старый `Free_SEO_article_EN` полезен только как prototype/reference. В нём есть
двухпроходная идея `facts + outline -> draft`, retry и run manifest, но нет topic
discovery, translation, CMS integration, durable queue и безопасной публикации.
Его URL fetcher, model selection, citation handling и QA нельзя переносить в
production без переписывания.

## Decision

### CMS и hosting

Выбираем:

- Astro static site;
- TypeScript content schema с runtime validation;
- Markdown/MDX content в Git;
- Cloudflare preview для code changes и pre-publish content build;
- Cloudflare production deploy только после successful verified content commit;
- private Git history и CI как audit/rollback boundary;
- локальный Mac mini runner для generation/translation jobs, когда нужна Ollama;
- Cloudflare/GitHub scheduled job только для лёгких discovery-задач без секретного
  или локального model runtime.

Cloudflare предпочтительнее для этого проекта: текущий сайт уже обслуживается через
Cloudflare, static requests укладываются в zero-cost модель, а custom domain не
требует переносить контент в proprietary CMS. Для нового deployment используется
рекомендованный Cloudflare путь Workers Static Assets. Если текущий неизвестный
origin окажется Cloudflare Pages, W0 отдельно сравнивает сохранение Pages с новой
Workers-конфигурацией. DNS и production deployment не меняются до успешного proof
of concept и отдельного разрешения владельца.

### Почему не традиционная CMS

Database-backed WordPress, Strapi, Directus и Sanity добавляют server/database,
auth, backup, API и patching без пользы для основного writer — Git-агента. Git уже
даёт diff, review, history, rollback, branch isolation и preview deployments.

Decap CMS остаётся обратимым optional layer, если появится реальная потребность в
визуальном human editor. Он не участвует в первом proof of concept.

### URL policy

Миграция не меняет URL существующего контента:

- English: `/{legacy-slug}`;
- другие локали: `/{locale}/{legacy-slug}`;
- category/tag/archive paths best-effort: их можно не переносить, а старые URL
  допускается отправлять на локализованный archive/home;
- `.html` aliases проверяются и при необходимости получают redirect;
- новые статьи могут использовать локализованные slugs только при наличии общей
  `translationKey` и корректных reciprocal `hreflang`;
- canonical и sitemap вычисляются из одного route manifest, а не задаются независимо.

### Content states

Новая статья проходит только вперёд по состояниям:

```text
discovered
  -> shortlisted
  -> researched
  -> drafted
  -> verified
  -> automatically_verified
  -> scheduled
  -> published_en
  -> translating
  -> published_locales

any state -> quarantined
```

Повторный запуск имеет стабильный `job_id` и не создаёт дубль. Ошибка публикации не
помечает статью опубликованной. Resume начинает с последнего атомарно завершённого
шага.

## Article schema

Минимальная metadata каждой локали:

```yaml
---
translationKey: local-ai-search-2026
locale: en
sourceLocale: en
title: "..."
description: "..."
slug: local-ai-search-2026
publishedAt: 2026-07-12T09:00:00Z
updatedAt: 2026-07-12T09:00:00Z
status: draft
author: Brainy
category: ai-tools
tags:
  - local-ai
generation:
  pipelineVersion: 1
  models:
    research: provider/model
    draft: provider/model
    edit: provider/model
sources:
  - id: S1
    title: "..."
    url: "https://..."
    accessedAt: 2026-07-12T08:00:00Z
---
```

Дополнительные поля: `legacyUrl`, `featuredImage`, image license/provenance,
`riskLevel`, `publishedBy: automation` и `automationDecision`. Canonical URL и `hreflang` выводятся из
route manifest, чтобы metadata не расходилась с реальными routes.

В публичном author block указывается Brainy. Рядом выводится проверяемая карточка
provenance: `Research: provider/model`, `Draft: provider/model`,
`Translation: provider/model`, pipeline version и дата. Модель берётся только из
зафиксированного run manifest: нельзя назвать модель, которая фактически не
участвовала. Author bio становится единым site-wide block; уникальные legacy bios
не являются migration gate. Raw prompts, полные страницы и скопированный competitor
text в Git не сохраняются.

## Topic discovery

Discovery adapters возвращают единый `TopicSignal`:

```text
title, url, source, observed_at, rank, engagement, locale, tags, license_hint
```

Начальный бесплатный набор:

- Hacker News API и официальные RSS;
- Reddit только через разрешённый OAuth/Data API, без HTML scraping: allowlist из
  10 subreddits, `hot/new` metadata, ETag/cache/jitter, no comment-body storage;
- Google News/Trends только через публично разрешённый feed/API и только как
  discovery signal;
- GitHub releases/search и официальные vendor/research RSS;
- arXiv API/RSS для research topics;
- YouTube Data API metadata и official channel feeds как discovery signal;
- Exploding Topics public monthly `trending-topics` post как low-frequency seed,
  без копирования текста или изображений.

Каждый adapter имеет rate limit, timeout, cache, user-agent/contact, robots/terms
policy и выключается отдельно. Неофициальный scraper, который обходит access
controls или создаёт непредсказуемый legal/operational risk, в production не входит.

Reddit poll запускается каждые 15 минут с jitter; пяти минут допускаются только
после подтверждения актуального API quota и при conditional requests. Reddit —
сигнал интереса, не источник фактов. Arbitrary third-party YouTube videos не
скачиваются и не транскрибируются: captions API требует авторизованный доступ к
caption track. Видео можно цитировать по публичным metadata/официальной странице;
полный transcript разрешён только для ролика Brainy либо при явном разрешении автора.

Темы deduplicate по canonical URL и semantic similarity, затем оцениваются по:

- соответствию нише Brainy;
- freshness и росту сигнала;
- возможности дать самостоятельную практическую ценность;
- наличию минимум двух независимых качественных источников;
- конкуренции и отличию от уже опубликованного;
- YMYL/safety/copyright risk.

Приоритет новой редакционной ниши: local AI, privacy, web research, Telegram
productivity, practical AI tooling и понятные технические объяснения. Finance,
medical, legal, gambling и иные high-stakes/YMYL темы не публикуются автоматически;
они идут в quarantine до отдельного решения владельца.

### Novelty and deduplication

До research и до любой LLM generation candidate сверяется с published corpus по
normalized entity + intent + locale-independent `topicKey`, lexical BM25 и local
embedding similarity title/summary. Candidate блокируется, если уже есть та же
задача/сущность или semantic similarity выше установленного threshold; причина и
ссылка на существующую статью остаются в job ledger.

Годовой refresh допускается, только когда в title/intent есть новый период
(`2026` после `2025`), прошлой статье достаточно времени, и новый evidence pack
демонстрирует material change: минимум три новых primary sources либо существенное
изменение фактов/версий/рынка. Такой материал обязан ссылаться на предыдущий обзор,
а не маскироваться под новую независимую статью.

## Research and writing

Research использует `SearchGateway -> SafePageFetcher -> EvidenceBundle` Brainy:

- provider-specific search data не попадает в writer;
- redirects и каждый target проходят public DNS/SSRF validation;
- ограничены content type, response bytes, pages, redirects, concurrency и deadline;
- page text считается недоверенным и не может менять system instructions;
- evidence получает стабильные IDs и canonical URLs;
- writer возвращает claims со ссылками только на разрешённые evidence IDs;
- финальные URL подставляет код; придуманные моделью URL отбрасываются.

Рекомендуемый writer flow:

1. evidence-bound fact table;
2. outline с привязкой разделов к fact/evidence IDs;
3. English draft;
4. редакторский проход без добавления новых фактов;
5. deterministic citation reconciliation;
6. quality and risk gates.

Provider policy остаётся zero-cost и task-specific:

- provider/model фиксируется на весь шаг;
- remote model допускается только после `discovered -> eligible -> canary -> active`;
- проверяются все price dimensions, а не суффикс имени;
- NVIDIA/OpenRouter free quota exhaustion не вызывает платный fallback;
- локальная Ollama — гарантированный fallback и основной путь для массовых
  переводов;
- generation concurrency по умолчанию 1;
- article jobs запускаются с низким приоритетом и не должны ухудшать Telegram SLO.

## Translation

English публикуется первым. Переводы создаются независимыми resumable jobs для всех
семи остальных локалей и сохраняют тот же `translationKey`.

Translation gate проверяет:

- полноту заголовка, description, body, CTA и source labels;
- сохранность evidence IDs и URL;
- отсутствие новых фактов;
- locale и отсутствие untranslated placeholder blocks;
- semantic equivalence ключевых claims;
- reciprocal `hreflang`, canonical и locale navigation;
- локализованные category/tag labels из общего словаря.

Один неудачный перевод quarantines только эту локаль и не снимает уже проверенную
English-версию.

## Quality gates

Статья не может перейти в `automatically_verified`, если не выполнены:

- schema/frontmatter validation;
- минимум два независимых источника для проверяемых claims;
- каждый materially verifiable claim имеет реальный evidence ID;
- citation IDs существуют, URL входят в allowlist и проходят canonicalization;
- link checker не находит malformed/blocked URLs;
- title/description/heading/alt-text checks;
- near-duplicate и source-similarity check против corpus;
- отсутствие длинных verbatim fragments из источников;
- prompt-injection fixtures не меняют policy;
- risk policy не разрешает auto-publish YMYL;
- production Astro build и route manifest tests green;
- preview smoke проверяет page, language switch, canonical, `hreflang`, JSON-LD,
  sitemap и Telegram CTA.

Публикация автономна: успешно прошедшая low-risk статья автоматически коммитится,
build/preview проверяется и затем публикуется scheduler'ом. Нет human approval step.
Вместо него действуют quarantine, one-article daily cap на старте, hard kill switch,
идемпотентность и automatic rollback при deployment/validation failure. High-risk
темы никогда не обходят quarantine.

## Migration plan

Миграция строится как повторяемый extractor, а не одноразовый ручной перенос.

1. Зафиксировать read-only manifest всех текущих URLs, status codes, canonical,
   `hreflang`, title, description и content hash.
2. Парсить только semantic containers текущего Flex/Pelican HTML: article header,
   body, source list, author block, category, tags и locale switcher.
3. Связать восемь языковых версий общим `translationKey`, используя legacy slug.
4. Скопировать все используемые article images, если локальный asset доступен, и
   записать provenance; отсутствующий/неиспользуемый asset — warning, не blocker.
5. Преобразовать Donate/Terms отдельно от articles.
6. Создать category/tag mapping только как best-effort navigation; legacy taxonomy
   URLs не являются content migration gate.
7. Сгенерировать Astro content и route manifest.
8. Сравнить normalized rendered body с legacy HTML и сохранить migration report.
9. Проверить 80 article documents, 16 legal/page documents, CTA и использованные
   images; legacy taxonomy проверяется отдельно как non-blocking report.
10. Переключать production только после preview review и сохранённого rollback.

Legacy repository/commit остаётся immutable rollback snapshot. Старые HTML не
удаляются до подтверждения production crawl и отсутствия критических regressions.

## Vertical slices

### Slice W0 — decision and inventory

Acceptance criteria:

- этот ADR принят;
- зафиксирован полный legacy URL/content/assets manifest;
- выбран и создан private repository boundary до добавления publisher code;
- известен фактический Cloudflare deployment source;
- выбран Pages-retain или Workers Static Assets с зафиксированной причиной;
- production DNS/deploy не менялись.

### Slice W1 — two-article Astro proof of concept

Scope: две существующие статьи, `en` + `ru`, без production deploy.

Acceptance criteria:

- pinned Node/package manager и lockfile;
- typed content collection;
- старые article URLs для четырёх страниц совпадают;
- canonical, reciprocal `hreflang`, `x-default`, sitemap, RSS и Article JSON-LD;
- language switch и Telegram CTA работают;
- deterministic migration report;
- lint/typecheck/test/build одной командой;
- Cloudflare preview доступен только после отдельного разрешения на push/deploy.

### Slice W2 — complete migration

Acceptance criteria:

- все 10 статей × 8 локалей и 2 legal pages × 8 локалей перенесены;
- CTA и все доступные используемые article images перенесены;
- category/tag/archive migration выпускает report, но не блокирует cutover;
- старый и новый normalized article body эквивалентны;
- route/canonical/hreflang matrix green;
- production ещё не переключён.

### Slice W3 — pipeline core, automatic low-risk topic input

Scope: безопасный research/write pipeline для заданной темы, без cron и publish.

Acceptance criteria:

- typed job/evidence/article contracts;
- fake providers и recorded HTTP fixtures;
- safe fetch/prompt injection/citation/price fail-closed tests;
- facts -> outline -> draft -> QA resume;
- output — verified publish-ready Markdown с actual model provenance;
- никаких сетевых запросов в обычных тестах.

### Slice W4 — discovery adapters and scoring

Acceptance criteria:

- минимум два permitted free discovery adapters;
- normalized `TopicSignal`, dedupe и score explanation;
- rate limit/cache/terms controls;
- YMYL auto-publish prohibition;
- Reddit official API adapter and Exploding Topics monthly adapter с terms/cache policy;
- YouTube metadata-only adapter; no third-party transcript harvesting;
- novelty/deduplication gate до research и drafting.

### Slice W5 — autonomous publishing canary

Acceptance criteria:

- idempotent content commit and deploy request from the private repository;
- preview/build/QA result фиксируется в job ledger;
- no human approval step for low-risk content;
- publish status проверяется, а не предполагается;
- retry не создаёт duplicate article;
- rollback документирован.

### Slice W6 — asynchronous translations

Acceptance criteria:

- семь locale jobs resume независимо;
- source URLs/evidence IDs неизменны;
- locale parity и semantic checks green;
- partial translation failure не ломает English page;
- model attribution отражает фактически использованные модели.

### Slice W7 — scheduled operation

Acceptance criteria:

- durable SQLite job ledger без хранения raw crawled pages;
- lock, idempotency, bounded concurrency 1, retry budget и quarantine;
- запуск только в разрешённые окна и с учётом загрузки Ollama;
- structured metrics без article body/raw page text в логах;
- external quotas учитываются отдельно от Telegram bot;
- auto-publish включён только для `automatically_verified` low-risk content.

### Slice W8 — autonomous volume ramp

Acceptance criteria:

- достаточно большой reviewed sample без P0/P1 defects;
- только low-risk niche и allowlisted source classes;
- kill switch и rollback;
- дневной/недельный publish cap;
- YMYL и legal/privacy-policy content остаются manual-only.

## First implementation order

Начать с `W0 -> W1`. Не подключать cron, Reddit/Google scraping, массовую миграцию
или uncontrolled crawling до green proof of concept. После W1 решение об Astro обратимо: Markdown
и migration manifest можно перенести в Hugo или другой static generator без потери
контента.

## Evidence

- [Astro content collections](https://docs.astro.build/en/guides/content-collections/)
  поддерживают typed schema и build-time validation.
- [Astro internationalization](https://docs.astro.build/en/guides/internationalization/)
  поддерживает locale routing и fallback; правила связи переводов остаются в нашей
  content schema.
- [Astro deployment on Cloudflare](https://docs.astro.build/en/guides/deploy/cloudflare/)
  указывает Workers Static Assets как рекомендуемый путь для новых проектов.
- [Cloudflare Static Assets billing and limits](https://developers.cloudflare.com/workers/static-assets/billing-and-limitations/)
  и [Workers limits](https://developers.cloudflare.com/workers/platform/limits/)
  подтверждают zero-cost suitability статического сайта в пределах documented limits.
- Если W0 подтвердит существующий Pages project, применяются
  [Cloudflare Pages limits](https://developers.cloudflare.com/pages/platform/limits/).
- [GitHub Actions schedule](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
  может задерживаться и отключается при длительной неактивности, поэтому scheduled
  workflow должен иметь manual dispatch, idempotency и внешний health signal.
- [Decap i18n](https://decapcms.org/docs/i18n/) и
  [editorial workflow](https://decapcms.org/docs/editorial-workflows/)
  подтверждают возможность добавить Git-based visual UI позже.
- [GitHub Pages limits](https://docs.github.com/en/pages/getting-started-with-github-pages/github-pages-limits),
  [Vercel Hobby terms](https://vercel.com/docs/plans/hobby) и
  [Netlify pricing](https://www.netlify.com/pricing/) делают эти варианты менее
  надёжными для strict zero-budget product site с регулярными production builds.

## Reversal plan

- Если Astro proof of concept не проходит URL/i18n/build gates, сравнить Hugo на том
  же content manifest; extraction и Markdown не выбрасываются.
- Если Cloudflare Pages limits или integration не подходят, тот же static output
  переносится на другой host без смены content model.
- Если agents-only editing неудобен людям, добавить Decap CMS поверх Git.
- Если генератор систематически не проходит factual QA, оставить discovery и draft
  automation, а approval/publish — полностью ручными.

## Actions requiring explicit owner approval

- push новой branch или pull request;
- preview/production deploy и привязка Cloudflare account;
- изменение DNS/custom domain;
- новые внешние credentials или передача данных third-party provider;
- расходы, платные endpoints или изменение privacy policy.
