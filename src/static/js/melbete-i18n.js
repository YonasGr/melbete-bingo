// Melbete Bingo - English / Amharic language layer (player Mini App hub).
//
// Static text: put data-i18n="key" (text), data-i18n-ph (placeholder), data-i18n-aria
// (aria-label) on the element. Dynamic text: call mbT("key", {n: 5}).
// Choice is remembered in localStorage and announced with a "melbete:lang" event.
// Server error messages (e.message from the API) are still English - they come from the backend.
(() => {
  "use strict";
  const KEY = "melbete_lang";

  const D = {
    en: {
      // wallet + nav
      bal_label: "Available balance", view_history: "View history",
      deposit: "Deposit", withdraw: "Withdraw",
      tab_games: "Games", tab_promos: "Promos", tab_deposit: "Deposit", tab_withdraw: "Withdraw", tab_profile: "Profile",
      chip_balance: "Balance", chip_etb: "ETB",
      play_title: "Play Bingo", play_sub: "Join a live Bingo round.", avail_one: "1 available",
      game_name: "Bingo", game_desc: "Live community rounds", play: "Play",
      splash_tag: "Play. Win. Cash out.", lang_switch: "Change language", close: "Close",
      // deposit modal
      dep_title: "💳 Deposit",
      dep_intro: "Choose a payment method, send the money, then attach the transaction SMS and a screenshot so your payment is confirmed quickly.",
      dep_s1: "Choose the payment method you will send money with",
      dep_s2: "Enter the amount you sent",
      dep_s3: "Confirm your payment with the SMS and a screenshot",
      dep_amount: "Amount (ETB)", dep_amount_ph: "e.g. 200",
      dep_sms: "Payment SMS message", required: "*required",
      dep_sms_ph: "Paste the full transaction SMS you received from Telebirr or CBE Birr here. Include the transaction link if it has one.",
      dep_shot: "Payment screenshot", dep_shot_hint: "📎 Tap here to attach a screenshot",
      dep_warn: "⚠️ The transaction SMS and screenshot are required. If the SMS has a transaction link, we check that the payment was not used before; otherwise an admin confirms it by hand.",
      dep_submit: "I've sent the money",
      dep_foot: "Once an admin confirms your payment, the money will arrive in your wallet shortly.",
      dep_limits: "Min {min} ETB · Max {max} ETB",
      // withdraw modal
      wd_title: "🏧 Withdraw",
      wd_s1: "Choose where the money should go",
      wd_s2: "Confirm your account details and the amount to withdraw",
      wd_account: "{name} account number", wd_account_default: "Payment",
      wd_account_ph: "e.g. 09XXXXXXXX", wd_save: "Save account number",
      wd_legal: "Full name registered on the payment account", wd_legal_ph: "Enter it exactly as registered on the account",
      wd_amount: "Amount to withdraw (ETB)", wd_amount_ph: "e.g. 300",
      wd_submit: "Send withdrawal request",
      wd_foot: "An admin reviews every withdrawal request and then sends the payment by hand once confirmed.",
      wd_limits: "You can withdraw only what you've won: {won} ETB. Min {min} ETB · Max {max} ETB. Money you deposit is for playing; it can't be withdrawn directly.",
      // progress bar labels
      st_method: "Method", st_amount: "Amount", st_proof: "Proof", st_account: "Account", st_details: "Details",
      // receipt
      rc_title_dep: "Deposit request sent", rc_title_wd: "Withdrawal request sent",
      rc_amount: "Amount", rc_ref: "Reference", rc_method: "Method", rc_time: "Time", rc_status: "Status",
      rc_pending: "Pending review", rc_copy: "Copy reference", rc_copied: "Reference copied", rc_done: "Done",
      rc_note_dep: "We're checking your payment. You can close this screen; the status updates on your home screen.",
      rc_note_wd: "Your winnings are on hold for this request. You can close this screen; the status updates on your home screen.",
      // tracker
      tr_requested: "Requested", tr_reviewing: "Reviewing", tr_credited: "Credited", tr_notcredited: "Not credited",
      tr_paid: "Paid", tr_refunded: "Refunded",
      tr_dep: "Deposit", tr_wd: "Withdrawal",
      tr_note_dep_pending: "We're checking your payment. You'll see the money here as soon as it arrives; you can close this page.",
      tr_note_dep_done: "The money is in your wallet. Good luck!",
      tr_note_dep_fail: "We couldn't confirm this payment. Please contact support.",
      tr_note_wd_pending: "An admin is reviewing your request and will send the payment by hand.",
      tr_note_wd_done: "Your payment has been sent. Thank you for playing!",
      tr_note_wd_fail: "The request was declined and the money is back in your wallet.",
      ago_now: "just now", ago_min: "{n} min ago", ago_hr: "{n} hr ago", ago_day: "{n} d ago",
      // history status
      s_pending: "Pending", s_completed: "Completed", s_rejected: "Rejected",
      history_empty: "No transactions yet.",
      // toasts
      tt_methods_fail: "Couldn't load payment methods. Please try again.",
      tt_no_dep_methods: "No deposit method is set up right now. Please contact support.",
      tt_no_wd_methods: "No withdrawal method is set up right now. Please contact support.",
      tt_pick_method: "Choose a payment method first.",
      tt_need_amount: "Enter the amount.", tt_need_sms: "Paste the full transaction SMS; it's required.",
      tt_need_shot: "Attach the transaction screenshot; a deposit can't be sent without it.",
      tt_shot_big: "The screenshot must be smaller than 6 MB.",
      tt_need_wd_method: "Choose where the money should go first.", tt_need_address: "Enter the account number the money should go to.",
      tt_addr_saved: "Account number saved.", tt_need_wd_amount: "Enter the amount you want to withdraw.",
      tt_need_legal: "Enter the full name registered on the payment account.",
      tt_dep_sent: "Deposit request sent. We'll confirm it shortly.", tt_wd_sent: "Withdrawal request sent.",
      dep_pick_account: "Choose the {name} account to send to:", dep_holder: "Account holder: {name}", dep_holder_none: "not provided",
      copy: "Copy", copied: "Copied",
      // onboarding
      ob_skip: "Skip", ob_next: "Next", ob_start: "Start playing",
      ob1_title: "Welcome to Melbete Bingo", ob1_text: "Choose your language. You can change it any time from the top bar.",
      ob2_title: "How a round works",
      ob2_a: "Pick your cartela before the countdown ends.", ob2_b: "Watch the numbers being called live.", ob2_c: "Complete the winning line first and take the prize.",
      ob3_title: "Your money, clearly",
      ob3_a: "Deposit to play.", ob3_a2: "Money you deposit is for playing rounds.",
      ob3_b: "Withdraw your winnings.", ob3_b2: "Prizes you win can be withdrawn.",
      ob3_c: "Track every request.", ob3_c2: "Each deposit and withdrawal shows its status on your home screen.",
    },

    am: {
      bal_label: "ያለዎት ቀሪ ሂሳብ", view_history: "ታሪክ ይመልከቱ",
      deposit: "ገንዘብ ያስገቡ", withdraw: "ገንዘብ ያውጡ",
      tab_games: "ጨዋታዎች", tab_promos: "ሽልማቶች", tab_deposit: "ማስገቢያ", tab_withdraw: "ማውጫ", tab_profile: "መገለጫ",
      chip_balance: "ሂሳብ", chip_etb: "ብር",
      play_title: "ቢንጎ ይጫወቱ", play_sub: "በቀጥታ ወደሚካሄድ የቢንጎ ዙር ይቀላቀሉ።", avail_one: "1 ይገኛል",
      game_name: "ቢንጎ", game_desc: "የቀጥታ የማህበረሰብ ዙሮች", play: "ይጫወቱ",
      splash_tag: "ይጫወቱ። ያሸንፉ። ገንዘብዎን ይውሰዱ።", lang_switch: "ቋንቋ ቀይር", close: "ዝጋ",
      dep_title: "💳 ገንዘብ ማስገቢያ",
      dep_intro: "የመክፈያ ዘዴዎን ይምረጡ፣ ገንዘቡን ይላኩ፣ ከዚያም ክፍያዎ በፍጥነት እንዲረጋገጥ የግብይቱን ኤስኤምኤስ እና ስክሪንሾት ያያይዙ።",
      dep_s1: "ገንዘብ የሚልኩበትን የመክፈያ ዘዴ ይምረጡ",
      dep_s2: "የላኩትን የገንዘብ መጠን ያስገቡ",
      dep_s3: "በኤስኤምኤስ እና በስክሪንሾት ክፍያዎን ያረጋግጡ",
      dep_amount: "የገንዘብ መጠን (ብር)", dep_amount_ph: "ለምሳሌ፦ 200",
      dep_sms: "የክፍያ ኤስኤምኤስ መልዕክት", required: "*ያስፈልጋል",
      dep_sms_ph: "ከቴሌብር ወይም ከሲቢኢ ብር የደረሰዎትን የግብይት ኤስኤምኤስ ሙሉውን እዚህ ይለጥፉ። የግብይት ማስፈንጠሪያ ካለው አብሮ መካተት ይችላል።",
      dep_shot: "የክፍያ ስክሪንሾት", dep_shot_hint: "📎 ስክሪንሾት ለማያያዝ እዚህ ይንኩ",
      dep_warn: "⚠️ የግብይቱን ኤስኤምኤስ እና ስክሪንሾት ማያያዝ ግዴታ ነው። ኤስኤምኤሱ የግብይት ማስፈንጠሪያ ካለው ክፍያው ከዚህ በፊት ጥቅም ላይ እንዳልዋለ ይፈተሻል፤ ከሌለው ደግሞ አስተዳዳሪ በእጅ ያረጋግጠዋል።",
      dep_submit: "ገንዘቡን ልኬያለሁ",
      dep_foot: "አስተዳዳሪው ክፍያዎን ካረጋገጠ በኋላ ገንዘቡ በቅርቡ ወደ ዋሌትዎ ይገባል።",
      dep_limits: "ዝቅተኛው {min} ብር · ከፍተኛው {max} ብር",
      wd_title: "🏧 ገንዘብ ማውጫ",
      wd_s1: "ገንዘቡ የሚገባበትን የመክፈያ ዘዴ ይምረጡ",
      wd_s2: "የሂሳብ መረጃዎን እና የሚያወጡትን መጠን ያረጋግጡ",
      wd_account: "{name} ሂሳብ ቁጥር", wd_account_default: "የመክፈያ",
      wd_account_ph: "ለምሳሌ፦ 09XXXXXXXX", wd_save: "የሂሳብ ቁጥሩን ያስቀምጡ",
      wd_legal: "በመክፈያ ሂሳቡ ላይ የተመዘገበው ሙሉ ስም", wd_legal_ph: "በሂሳቡ ላይ እንደተመዘገበው በትክክል ያስገቡ",
      wd_amount: "የሚያወጡት የገንዘብ መጠን (ብር)", wd_amount_ph: "ለምሳሌ፦ 300",
      wd_submit: "ገንዘብ ለማውጣት ጥያቄ ይላኩ",
      wd_foot: "የገንዘብ ማውጣት ጥያቄዎችን አስተዳዳሪ ይመረምራል፤ ከተረጋገጠ በኋላም ክፍያውን በእጅ ይፈጽማል።",
      wd_limits: "ማውጣት የሚችሉት የአሸነፉትን ገንዘብ ብቻ ነው፦ {won} ብር። ዝቅተኛው {min} ብር · ከፍተኛው {max} ብር። ያስገቡትን ገንዘብ ለጨዋታ መጠቀም ይችላሉ፤ በቀጥታ ማውጣት ግን አይችሉም።",
      st_method: "ዘዴ", st_amount: "መጠን", st_proof: "ማረጋገጫ", st_account: "ሂሳብ", st_details: "መረጃ",
      rc_title_dep: "የገንዘብ ማስገቢያ ጥያቄ ተልኳል", rc_title_wd: "የገንዘብ ማውጫ ጥያቄ ተልኳል",
      rc_amount: "መጠን", rc_ref: "ቁጥር", rc_method: "ዘዴ", rc_time: "ሰዓት", rc_status: "ሁኔታ",
      rc_pending: "ግምገማ በመጠባበቅ ላይ", rc_copy: "ቁጥሩን ቅዳ", rc_copied: "ቁጥሩ ተቀድቷል", rc_done: "ተጠናቋል",
      rc_note_dep: "ክፍያዎን እያረጋገጥን ነው። ይህን ገጽ መዝጋት ይችላሉ፤ ሁኔታው በመነሻ ገጽዎ ላይ ይታደሳል።",
      rc_note_wd: "ለዚህ ጥያቄ ያሸነፉት ገንዘብ ተይዟል። ይህን ገጽ መዝጋት ይችላሉ፤ ሁኔታው በመነሻ ገጽዎ ላይ ይታደሳል።",
      tr_requested: "ተጠይቋል", tr_reviewing: "እየተረጋገጠ ነው", tr_credited: "ገብቷል", tr_notcredited: "አልገባም",
      tr_paid: "ተከፍሏል", tr_refunded: "ተመልሷል",
      tr_dep: "ገንዘብ ማስገቢያ", tr_wd: "ገንዘብ ማውጫ",
      tr_note_dep_pending: "ክፍያዎን እያረጋገጥን ነው። ገንዘቡ እንደገባ እዚህ ያያሉ፤ ገጹን መዝጋት ይችላሉ።",
      tr_note_dep_done: "ገንዘቡ ወደ ዋሌትዎ ገብቷል። መልካም ጨዋታ!",
      tr_note_dep_fail: "ክፍያው ሊረጋገጥ አልቻለም። እባክዎ የድጋፍ ቡድኑን ያነጋግሩ።",
      tr_note_wd_pending: "አስተዳዳሪ ጥያቄዎን እየመረመረ ነው፤ ክፍያውንም በእጅ ይልካል።",
      tr_note_wd_done: "ክፍያዎ ተልኳል። ስለተጫወቱ እናመሰግናለን!",
      tr_note_wd_fail: "ጥያቄው ተቀባይነት አላገኘም፤ ገንዘቡም ወደ ዋሌትዎ ተመልሷል።",
      ago_now: "ልክ አሁን", ago_min: "ከ{n} ደቂቃ በፊት", ago_hr: "ከ{n} ሰዓት በፊት", ago_day: "ከ{n} ቀን በፊት",
      s_pending: "በመጠባበቅ ላይ", s_completed: "ተጠናቋል", s_rejected: "ውድቅ ሆኗል",
      history_empty: "እስካሁን ምንም ግብይት የለም።",
      tt_methods_fail: "የመክፈያ ዘዴዎችን መጫን አልተቻለም። እባክዎ እንደገና ይሞክሩ።",
      tt_no_dep_methods: "በአሁኑ ጊዜ ገንዘብ ማስገቢያ የመክፈያ ዘዴ አልተዘጋጀም። እባክዎ የድጋፍ ቡድኑን ያነጋግሩ።",
      tt_no_wd_methods: "በአሁኑ ጊዜ ገንዘብ ማውጫ የመክፈያ ዘዴ አልተዘጋጀም። እባክዎ የድጋፍ ቡድኑን ያነጋግሩ።",
      tt_pick_method: "መጀመሪያ ገንዘብ የሚልኩበትን የመክፈያ ዘዴ ይምረጡ።",
      tt_need_amount: "የላኩትን የገንዘብ መጠን ያስገቡ።", tt_need_sms: "የግብይቱን ኤስኤምኤስ መልዕክት ሙሉውን ይለጥፉ፤ ይህ መረጃ ያስፈልጋል።",
      tt_need_shot: "የግብይቱን ስክሪንሾት ያያይዙ፤ ያለ ስክሪንሾት የገንዘብ ማስገቢያ ጥያቄውን መላክ አይቻልም።",
      tt_shot_big: "የመረጡት ስክሪንሾት ከ6 ሜጋባይት መብለጥ የለበትም።",
      tt_need_wd_method: "መጀመሪያ ገንዘቡ የሚገባበትን የመክፈያ ዘዴ ይምረጡ።", tt_need_address: "ገንዘቡ የሚገባበትን የሂሳብ ቁጥር ያስገቡ።",
      tt_addr_saved: "የሂሳብ ቁጥሩ ተቀምጧል።", tt_need_wd_amount: "ማውጣት የሚፈልጉትን የገንዘብ መጠን ያስገቡ።",
      tt_need_legal: "በመክፈያ ሂሳቡ ላይ የተመዘገበውን ሙሉ ስም ያስገቡ።",
      tt_dep_sent: "የገንዘብ ማስገቢያ ጥያቄ ተልኳል። በቅርቡ እናረጋግጣለን።", tt_wd_sent: "የገንዘብ ማውጫ ጥያቄ ተልኳል።",
      dep_pick_account: "ገንዘቡን የሚልኩበትን {name} ሂሳብ ይምረጡ፦", dep_holder: "የሂሳብ ባለቤት፦ {name}", dep_holder_none: "አልተገለጸም",
      copy: "ቅዳ", copied: "ተቀድቷል",
      ob_skip: "ዝለል", ob_next: "ቀጣይ", ob_start: "መጫወት ይጀምሩ",
      ob1_title: "ወደ Melbete Bingo እንኳን በደህና መጡ", ob1_text: "ቋንቋዎን ይምረጡ። በማንኛውም ጊዜ ከላይኛው አሞሌ መቀየር ይችላሉ።",
      ob2_title: "ዙሩ እንዴት ይካሄዳል",
      ob2_a: "ቆጠራው ከማለቁ በፊት ካርቴላዎን ይምረጡ።", ob2_b: "ቁጥሮች በቀጥታ ሲጠሩ ይከታተሉ።", ob2_c: "አሸናፊውን መስመር መጀመሪያ ያጠናቅቁ፤ ሽልማቱንም ይውሰዱ።",
      ob3_title: "ገንዘብዎ በግልጽ",
      ob3_a: "ለመጫወት ገንዘብ ያስገቡ።", ob3_a2: "ያስገቡት ገንዘብ ለዙሮች መጫወቻ ነው።",
      ob3_b: "ያሸነፉትን ያውጡ።", ob3_b2: "ያሸነፉት ሽልማት ማውጣት ይቻላል።",
      ob3_c: "እያንዳንዱን ጥያቄ ይከታተሉ።", ob3_c2: "እያንዳንዱ ገንዘብ ማስገባት እና ማውጣት ሁኔታው በመነሻ ገጽዎ ላይ ይታያል።",
    },
  };

  let lang = "am";
  try { const s = localStorage.getItem(KEY); if (s === "en" || s === "am") lang = s; } catch (e) { /* storage blocked */ }

  function mbT(key, vars) {
    let s = (D[lang] && D[lang][key]) || D.en[key] || key;
    if (vars) for (const k in vars) s = s.split("{" + k + "}").join(String(vars[k]));
    return s;
  }

  function apply(root) {
    const r = root || document;
    r.querySelectorAll("[data-i18n]").forEach((el) => { el.textContent = mbT(el.dataset.i18n); });
    r.querySelectorAll("[data-i18n-ph]").forEach((el) => { el.placeholder = mbT(el.dataset.i18nPh); });
    r.querySelectorAll("[data-i18n-aria]").forEach((el) => { el.setAttribute("aria-label", mbT(el.dataset.i18nAria)); });
    document.documentElement.lang = lang === "am" ? "am" : "en";
  }

  function setLang(l, silent) {
    if (l !== "en" && l !== "am") return;
    lang = l;
    try { localStorage.setItem(KEY, l); } catch (e) { /* ignore */ }
    apply();
    if (!silent) document.dispatchEvent(new CustomEvent("melbete:lang", { detail: { lang: l } }));
  }

  window.mbT = mbT;
  window.MB_I18N = { t: mbT, apply, setLang, get lang() { return lang; }, has: () => { try { return !!localStorage.getItem(KEY); } catch (e) { return false; } } };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => apply());
  else apply();
})();
