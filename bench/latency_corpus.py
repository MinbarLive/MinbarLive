"""Representative Arabic utterances for the translation-latency benchmark.

Real sermon-context text (not filler), so translation quality stays judgeable
in the --compare diff. Each entry: {"id", "kategorie", "arabisch", "gloss"}.
``gloss`` is a rough German meaning for the human quality check only — it is
never sent to the API.

Category coverage (per the work order):
- predigt_*   : everyday case, short/medium/long normal preaching
- quran_hint  : Quran wording that should hit the RAG *hint* band
                (RAG_MIN_SIMILARITY 0.60 .. RAG_HARD_MATCH_THRESHOLD 0.85),
                NOT a clean full verse — a clean full verse triggers the
                verified-verse bypass (no LLM call) and would not exercise the
                translation path at all. The runner flags any entry whose
                output comes back with the verified marker so it can be swapped.
- codeswitch  : Arabic with embedded German (tests pass-through handling)
- dauersatz   : >30 words, worst case for the LLM generation stage
- fragment    : 2-4 words, the coalescing / short-utterance class
"""

from __future__ import annotations

CORPUS: list[dict[str, str]] = [
    # --- normal preaching: the everyday case -------------------------------
    {
        "id": "predigt_short_1",
        "kategorie": "predigt_short",
        "arabisch": "الحمد لله رب العالمين.",
        "gloss": "Lob sei Gott, dem Herrn der Welten.",
    },
    {
        "id": "predigt_short_2",
        "kategorie": "predigt_short",
        "arabisch": "اتقوا الله عباد الله.",
        "gloss": "Fürchtet Gott, ihr Diener Gottes.",
    },
    {
        "id": "predigt_medium_1",
        "kategorie": "predigt_medium",
        "arabisch": "أوصيكم ونفسي بتقوى الله في السر والعلن، فإنها وصية الله للأولين والآخرين.",
        "gloss": "Ich ermahne euch und mich zur Gottesfurcht im Verborgenen "
        "und Offenen; sie ist Gottes Gebot an die Früheren und Späteren.",
    },
    {
        "id": "predigt_medium_2",
        "kategorie": "predigt_medium",
        "arabisch": "إن هذه الدنيا دار ممر لا دار مقر، فاعملوا لآخرتكم قبل أن يأتي يوم لا ينفع فيه مال ولا بنون.",
        "gloss": "Diese Welt ist eine Durchgangs-, keine Bleibestätte; wirkt "
        "für euer Jenseits, bevor ein Tag kommt, an dem weder Geld noch Söhne nützen.",
    },
    {
        "id": "predigt_medium_3",
        "kategorie": "predigt_medium",
        "arabisch": "من أعظم أسباب سعادة القلب دوام ذكر الله تعالى والإكثار من الاستغفار في كل الأحوال.",
        "gloss": "Eine der größten Ursachen für Herzensglück ist das beständige "
        "Gedenken Gottes und das häufige Bitten um Vergebung in allen Lagen.",
    },
    # --- Quran wording in the *hint* band (must reach the LLM) --------------
    {
        "id": "quran_hint_1",
        "kategorie": "quran_hint",
        # Verse wording embedded inside a preacher's framing sentence, so the
        # segment is not "the verse alone" -> stays out of the verified bypass.
        "arabisch": "وقد قال ربنا في محكم تنزيله إن مع العسر يسرا فأبشروا بالفرج القريب.",
        "gloss": "Unser Herr sagte in Seiner Offenbarung: Mit der Härte kommt "
        "Erleichterung — so freut euch auf die nahe Erlösung.",
    },
    {
        "id": "quran_hint_2",
        "kategorie": "quran_hint",
        "arabisch": "واذكروا قوله سبحانه واصبروا إن الله مع الصابرين في كل زمان ومكان.",
        "gloss": "Gedenkt Seines Wortes: Habt Geduld, Gott ist mit den "
        "Geduldigen — zu jeder Zeit und an jedem Ort.",
    },
    # --- code-switching: Arabic with embedded German -----------------------
    {
        "id": "codeswitch_1",
        "kategorie": "codeswitch",
        "arabisch": "نحن اليوم في مدينة München نتحدث عن أهمية الصلاة في حياة المسلم.",
        "gloss": "Wir sind heute in München und sprechen über die Bedeutung "
        "des Gebets im Leben des Muslim.",
    },
    {
        "id": "codeswitch_2",
        "kategorie": "codeswitch",
        "arabisch": "الأخ Ahmad سيقوم بترجمة الخطبة إلى اللغة الألمانية بعد قليل.",
        "gloss": "Der Bruder Ahmad wird die Predigt gleich ins Deutsche übersetzen.",
    },
    # --- long unbroken sentence: worst case for the LLM stage --------------
    {
        "id": "dauersatz_1",
        "kategorie": "dauersatz",
        "arabisch": "اعلموا رحمكم الله أن الإنسان في هذه الحياة الدنيا مبتلى بالخير والشر "
        "والغنى والفقر والصحة والمرض ليظهر صدق إيمانه وحسن توكله على ربه، "
        "فمن صبر عند البلاء وشكر عند الرخاء فقد فاز فوزا عظيما ونال رضا الله في الدارين.",
        "gloss": "Wisset, dass der Mensch in diesem Leben mit Gutem und Bösem, "
        "Reichtum und Armut, Gesundheit und Krankheit geprüft wird, damit sich "
        "sein Glaube zeige; wer bei Prüfung geduldig und bei Wohlstand dankbar "
        "ist, hat einen großen Gewinn erlangt.",
    },
    {
        "id": "dauersatz_2",
        "kategorie": "dauersatz",
        "arabisch": "وإن من علامات محبة العبد لربه أن يحافظ على الصلوات الخمس في أوقاتها مع الجماعة "
        "وأن يصل رحمه ويحسن إلى جيرانه ويكف أذاه عن الناس ويسعى في قضاء حوائج إخوانه "
        "المسلمين ما استطاع إلى ذلك سبيلا في ليله ونهاره.",
        "gloss": "Zu den Zeichen der Liebe des Dieners zu seinem Herrn gehört, "
        "die fünf Gebete zur Zeit in Gemeinschaft zu wahren, die "
        "Verwandtschaft zu pflegen, den Nachbarn Gutes zu tun und den "
        "Mitmenschen zu helfen, so viel er kann.",
    },
    # --- short fragments: the coalescing class -----------------------------
    {
        "id": "fragment_1",
        "kategorie": "fragment",
        "arabisch": "سبحان الله وبحمده.",
        "gloss": "Gepriesen sei Gott und Ihm sei Lob.",
    },
    {
        "id": "fragment_2",
        "kategorie": "fragment",
        "arabisch": "وبعد،",
        "gloss": "Und nun (rhetorische Überleitung),",
    },
]
