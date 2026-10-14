# Quran translation sources

Every `{code}.json` here maps the Arabic text of a verse (no diacritics, from
quranapi.pages.dev) to one established translation from [quranenc.com](https://quranenc.com),
with its reference appended: `"… (2:255)"`. When the app recognises a verse with high
confidence it shows this text verbatim instead of a model's translation. **A wrong file
therefore shows a wrong verse as verified.** Treat a change here as carefully as a change
to the verification code.

Build or rebuild with `notebooks/build_quran_dict.py code=key …`. The builder retries and
fails loudly, and strips footnote markers (`[1]`) and leading verse numbers. The first five
files predate that, so some of their entries still carry `[1]` markers.

| Code | quranenc key | Translation |
| --- | --- | --- |
| de | `german_bubenheim` | Frank Bubenheim |
| en | `english_hilali_khan` | Hilali & Khan |
| tr | `turkish_rwwad` | Rowwad Translation Center |
| sq | `albanian_nahi` | Hassan Nahi |
| bs | `bosnian_rwwad` | Rowwad Translation Center |
| az | `azeri_musayev` | Ali Khan Musayev |
| bn | `bengali_rwwad` | Rowwad Translation Center. Chosen over `bengali_zakaria`, whose verses are split around footnote markers |
| es | `spanish_garcia` | Isa García |
| fa | `persian_ih` | Rowwad Translation Center |
| fr | `french_rashid` | Rachid Maach. `french_montada` prefixes every verse with its number |
| ha | `hausa_gummi` | Abu Bakr Jumi |
| hi | `hindi_omari` | Azizul Haq al-Omari |
| id | `indonesian_affairs` | Ministry of Religious Affairs (Kemenag), the standard Indonesian edition |
| it | `italian_rwwad` | Rowwad Translation Center (served by the API, not in its list) |
| ja | `japanese_saeedsato` | Saeed Sato |
| ml | `malayalam_kunhi` | Abdul Hamid Haidar & Kunhi Muhammad |
| ms | `malay_basumayyah` | Abdullah Basumayyah (served by the API, not in its list) |
| nl | `dutch_center` | Rowwad Translation Center |
| pa | `punjabi_arif` | Arif Haleem. **Gurmukhi script**, the script models write for "Punjabi" |
| ps | `pashto_rwwad` | Rowwad Translation Center |
| pt | `portuguese_nasr` | Helmi Nasr |
| ru | `russian_rwwad` | Rowwad Translation Center (served by the API, not in its list) |
| so | `somali_yacob` | Abdullah Hasan Yaqoub |
| sv | `swedish_rwwad` | Rowwad Translation Center |
| sw | `swahili_rwwad` | Rowwad Translation Center |
| ta | `tamil_baqavi` | Abdulhamid Baqawi. **Not `tamil_omar`**: it returns sura 111's text under sura 112's metadata, so it passes every structural check while misaligned |
| th | `thai_rwwad` | Rowwad Translation Center |
| tl | `tagalog_rwwad` | Rowwad Translation Center |
| ur | `urdu_junagarhi` | Muhammad Junagarhi |
| vi | `vietnamese_rwwad` | Rowwad Translation Center |
| zh-hans | `chinese_makin` | Muhammad Makeen, Simplified script |

## Target languages without a file

The app translates verses for these with the model, as before:

- **zh-hant, ko, pl, sd**: quranenc has no translation (checked 2026-10-14). The Chinese
  one is Simplified only, and is not reused for Traditional readers.
- **ku**: the only source, `kurdish_bamoki`, is Sorani in Arabic script. The "Kurdish"
  target names no dialect, and most Kurdish speakers in Germany use Kurmanji in Latin
  script, so a Sorani file could put two scripts on one screen. Splitting the target
  into Kurmanji and Sorani would solve it.
