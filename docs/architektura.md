# Architektura aplikacji — 3 główne moduły

> Mapa nawigacji po redesignie. Po zalogowaniu użytkownik wybiera moduł
> (3 kafelki). Stary ogólny `/dashboard` zastąpiony ekranem wyboru modułu.

## Typy użytkowników

- **Wewnętrzni** (role: `user` < `manager` < `superuser` < `admin`) — dostęp do
  Zakupy / Transport + Admin wg roli.
- **Zewnętrzni spedytorzy** (nowa rola: `forwarder`) — dostęp **tylko** do portalu
  Spedycja, z izolacją danych (widzą wyłącznie swoje zlecenia).

## Strona startowa

Po zalogowaniu → wybór modułu (kafelki): **Zakupy · Transport · Spedycja**.
Zestaw kafelków zależny od roli (spedytor widzi tylko Spedycję).

---

## 📦 ZAKUPY (wewnętrzny)

- Porównanie dokumentów (`/analyze`)
- Agent proforma (`/agent/proforma`)
- Odprawa celna / SAD (`/sad`)
- Historia porównań (`/history`)
- Produkty (`/products`)
- Dostawcy (`/suppliers`)
- PDF → Excel (`/scan`)
- Artwork: porównanie (`/artwork`)
- Artwork: historia (`/artwork/history`)
- Artwork: szablony pól (`/artwork/profiles`)
- Artwork: Master Data (`/artwork/materials`)
- 🔗 współdzielone: Kolejka transportowa, Bundle, Magazyn, Baza wiedzy

## 🚢 TRANSPORT (wewnętrzny)

- Dostawy / Śledzenie (`/shipments`)
- Checklisty dokumentów (`/checklists`)
- Kalendarz ETD/ETA (`/calendar`)
- Śledzenie kontenerów (`/tracking`)
- Awizacja / Dispatch (`/transport/dispatch`)
- 🔗 współdzielone: Kolejka transportowa, Bundle, Magazyn, Baza wiedzy

## 🚛 SPEDYCJA (portal ZEWNĘTRZNY dla spedytorów)

Rola `forwarder`. Bez dostępu do wnętrza. Izolacja danych — spedytor widzi
tylko zlecenia wysłane do niego.

**Zakładka: Zlecenia spedycyjne**

Przepływ:

```
Transport ──(wysyła zlecenie spedycyjne)──► Portal Spedycja (spedytor)
                                                 │
                                                 ├─ przypisuje agenta celnego (pole tekstowe)
                                                 └─ zmienia status odprawy
                                                        │
                                                        ▼
                          aktualizacja wpada do KOLEJKI TRANSPORTOWEJ
                          (widok w Transport + Zakupy, ten sam rekord transport_queue)
```

Spedytor:
- widzi tylko zlecenia wysłane do niego z Transportu
- przypisuje **agenta celnego** (pole tekstowe)
- zmienia **status odprawy** → `transport_queue.customs_status`
  (`oczekuje` → `w_odprawie` → `zatwierdzone` / `odrzucone` → `wydane`)
- jego zmiany aktualizują wspólną Kolejkę transportową w Transport i Zakupy

## ⚙️ ADMIN (admin / manager)

- KPI (`/kpi`)
- Artwork KPI (`/artwork/kpi`)
- Koszty API (`/api-costs`)
- Biblioteka (`/library`)
- Admin (`/admin`)
- Szablony (`/templates`)
- Logi aktywności (`/activity`)
- 🔗 Baza wiedzy (współdzielona)

## Stopka (wszyscy wewnętrzni)

- Pomoc (`/help`)
- Profil (`/profile`)

---

## Zakładki współdzielone (między modułami)

| Zakładka | Moduły |
|---|---|
| Kolejka transportowa (`/transport/queue`) | Transport + Zakupy |
| Bundle (`/bundle`) | Zakupy + Transport |
| Magazyn (`/warehouse`) | Transport + Zakupy |
| Baza wiedzy (`/data`) | Admin + Zakupy + Transport |

## Pozycje specjalne

- **Zgłoszenia / tickets** (`/tickets`): wszyscy wewnętrzni; **ukryte dla
  spedytorów zewnętrznych**.
- **Słownik tłumaczeń** (`/translation-dictionary`): zagnieżdżony w Bazie wiedzy.
- **Waluty** (`/currencies`): usunięte z nawigacji.

---

## Konsekwencje techniczne (do implementacji)

1. Nowa rola `forwarder` (spedytor zewnętrzny) + filtrowanie nawigacji i danych.
2. Ekran wyboru modułu (3 kafelki) zależny od roli; zastępuje ogólny dashboard.
3. Pojęcie „zlecenie spedycyjne" = rekord `transport_queue` wysłany do spedytora
   (przypisanie spedytora + agenta celnego + obieg statusu odprawy).
4. Zakładki współdzielone respektują kontekst modułu, z którego wszedł użytkownik.
5. Izolacja danych w portalu Spedycja (spedytor widzi tylko swoje zlecenia).
