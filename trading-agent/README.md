# Trading Agent: trading guidato dalle news con Claude

Un agente di **paper trading** su **azioni ed ETF USA** e **forex**. Legge le news finanziarie, usa Claude per capire quali strumenti ne sono colpiti e in che direzione, poi apre e chiude posizioni in simulazione nel rispetto di regole di rischio fisse.

> ⚠️ Progetto sperimentale e didattico, non è una consulenza finanziaria. Gli URL di Alpaca e OANDA sono fissati sugli ambienti **paper/practice**: il codice non può inviare ordini con denaro reale.

## Come funziona

```
 Finnhub / Alpha Vantage        Claude (claude-opus-5-5)           codice deterministico
┌──────────────────────┐      ┌─────────────────────────┐      ┌──────────────────────────┐
│ news per titolo      │      │ per ogni news:          │      │ strategy.py              │
│ news di mercato/forex│ ───▶ │  strumenti impattati,   │ ───▶ │  score = Σ direzione ×   │
│ (deduplicate, SQLite)│      │  direzione, intensità,  │      │  intensità × confidenza ×│
└──────────────────────┘      │  confidenza, orizzonte  │      │  decadimento temporale   │
                              │ (JSON con schema fisso) │      │ risk.py                  │
                              └─────────────────────────┘      │  dimensione, limiti, stop│
                                                               └────────────┬─────────────┘
                                                                            ▼
                                                    broker: locale | Alpaca paper | OANDA practice
```

1. **Raccolta news**: news per ogni azione/ETF in watchlist e news di mercato generali e forex da Finnhub. In opzione, news con sentiment da Alpha Vantage. Ogni news viene salvata una volta sola in SQLite.
2. **Analisi con Claude** (`analyst.py`): le news nuove vanno a Claude in lotti. La risposta è vincolata da uno **schema JSON** che accetta solo i simboli della watchlist. Claude **non decide gli ordini**: dà solo una valutazione con la motivazione in italiano.
3. **Segnale** (`strategy.py`): per ogni strumento somma gli impatti recenti, pesati per intensità e confidenza. Ogni impatto perde peso nel tempo con un'emivita che dipende dall'orizzonte: 4 ore per intraday, 24 ore per giorni, 96 ore per settimane.
4. **Rischio** (`risk.py`): ha sempre l'ultima parola. Applica dimensione massima per posizione, esposizione lorda, numero di posizioni, stop loss, take profit, durata massima, cooldown, orari di mercato e **kill switch**. Il kill switch blocca le nuove entrate quando la perdita della giornata supera la soglia.
5. **Esecuzione**: prima si controllano le uscite, poi le entrate, a partire dai segnali più forti. Ogni decisione viene registrata nella tabella `decisions`, che fa da audit log.

## Installazione

```bash
cd trading-agent
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env              # inserisci le chiavi
cp config.example.toml config.toml
```

### Chiavi necessarie

| Variabile | Serve per | Dove ottenerla |
|---|---|---|
| `ANTHROPIC_API_KEY` | analisi delle news | console.anthropic.com |
| `FINNHUB_API_KEY` | news e prezzi azioni/ETF | finnhub.io (gratuito) |
| `ALPHAVANTAGE_API_KEY` | prezzi forex con broker locale; news opzionali | alphavantage.co (gratuito, 25 richieste/giorno) |
| `ALPACA_API_KEY_ID` / `ALPACA_API_SECRET_KEY` | solo con `brokers.equity = "alpaca"` | alpaca.markets → Paper Trading |
| `OANDA_API_TOKEN` / `OANDA_ACCOUNT_ID` | solo con `brokers.forex = "oanda"` | conto demo OANDA (in USD) |

Per iniziare bastano Anthropic, Finnhub e Alpha Vantage con il **broker locale**, un simulatore interno che non richiede alcun conto. Poi si può passare ad Alpaca e OANDA, che simulano l'esecuzione in modo più realistico.

## Uso

```bash
python -m trading_agent run-once --dry-run   # un ciclo: mostra cosa farebbe, non invia ordini
python -m trading_agent run-once             # un ciclo con ordini (paper)
python -m trading_agent loop --interval 900  # un ciclo ogni 15 minuti
python -m trading_agent status               # conto, posizioni, segnali, ultime decisioni
```

Esempio di output:

```
=== Ciclo 2026-11-17 16:00:00 UTC ===
News: 63 lette, 12 nuove, 12 analizzate da Claude

Segnali (score > 0 rialzista, < 0 ribassista):
  NVDA     +0.74  (2 news)
           +0.61 Nvidia beats estimates, guides above consensus — Ricavi data center sopra le attese…
  EUR_USD  -0.41  (1 news)

Azioni:
  - NVDA enter_long qty=+26 filled @ 141.2: notional ~3671 USD; …
```

## Configurazione

Tutto si regola in `config.toml`; i commenti in `config.example.toml` spiegano ogni voce. Le leve principali:

- `strategy.entry_threshold`: quanto deve essere forte il segnale per aprire una posizione. Più alto vuol dire meno operazioni.
- `risk.max_position_pct`, `risk.stop_loss_pct`, `risk.daily_loss_limit_pct`: quanto rischiare.
- `analyst.effort` e `analyst.batch_size`: qualità dell'analisi contro costo.
- `analyst.use_fallbacks`: se Claude rifiuta una richiesta, l'API la ripete su un modello di riserva. Il fallback avviene lato server, nella stessa chiamata.

### Costi indicativi di Claude

Un lotto di 8 news costa circa 3-4 mila token in input e qualche migliaio in output (ragionamento più JSON). Con `claude-opus-5-5` ($4 input / $20 output per milione di token) sono alcuni centesimi per lotto. Con il loop ogni 15 minuti e un flusso tipico di news, la spesa è di qualche dollaro al giorno. Per spendere meno si può abbassare `effort` a `low`, ridurre `max_news_per_cycle` o cambiare `model`. Sono stime: verifica il consumo reale nei log, che riportano i token di ogni chiamata.

## Test

```bash
python -m pytest -q
```

I test girano offline. Usano un client Claude finto, una fonte news finta e il broker locale, e coprono strategia, rischio, contabilità del simulatore e l'intero ciclo: entrata, stop loss, dry run, lotti falliti ritentati e rifiuti.

## Limiti noti

- **Google Finance** non ha un'API pubblica: per questo le fonti sono Finnhub e Alpha Vantage.
- Forex: sono supportate solo coppie con USD da un lato (EUR_USD, USD_JPY, …), non i cross.
- Il broker locale ignora le festività di borsa, simula lo spread solo come slippage fisso e usa un modello a margine semplificato.
- Le news arrivano con un ritardo di minuti, quindi la strategia non può competere sulla velocità. Il suo vantaggio può venire solo dall'interpretazione delle notizie su orizzonti di ore o giorni.
- Nessun backtest storico incluso: prima di fidarsi dei segnali conviene misurarne la qualità su dati passati.

## Struttura

```
trading_agent/
  __main__.py        CLI e assemblaggio dei componenti
  agent.py           ciclo: news → analisi → segnali → uscite → entrate
  analyst.py         prompt, schema JSON e chiamata a Claude
  strategy.py        calcolo dello score e decisione
  risk.py            dimensionamento e regole di rischio
  store.py           SQLite: news, impatti, decisioni
  data/              Finnhub, Alpha Vantage, raccolta news
  brokers/           locale, Alpaca paper, OANDA practice
tests/
```
