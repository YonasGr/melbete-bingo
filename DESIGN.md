---
name: Melbete Bingo
description: Real-time multiplayer Bingo on Telegram
colors:
  brand-deep: "#382d4c"
  brand-deep-light: "#554275"
  brand-accent: "#b527a1"
  brand-highlight: "#2bcbd3"
  brand-highlight-bright: "#01e5ee"
  brand-violet: "#6b43f4"
  success-green: "#10b981"
  danger-red: "#ef4444"
  neutral-bg: "#f6f7fb"
  surface: "#ffffff"
  ink: "#2b2538"
  border: "#e8e6ef"
  muted: "#766e88"
typography:
  display:
    fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif"
    fontSize: "clamp(2rem, 6vw, 3.5rem)"
    fontWeight: 800
    lineHeight: 1.15
  body:
    fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif"
    fontSize: "0.85rem"
    fontWeight: 400
    lineHeight: 1.5
  label:
    fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif"
    fontSize: "0.78rem"
    fontWeight: 700
    letterSpacing: "0.05em"
    textTransform: "uppercase"
rounded:
  sm: "0.4rem"
  md: "0.6rem"
  lg: "1rem"
  full: "999px"
spacing:
  xs: "0.3rem"
  sm: "0.5rem"
  md: "0.8rem"
  lg: "1rem"
  xl: "2rem"
components:
  button-primary:
    backgroundColor: "{colors.brand-accent}"
    textColor: "#ffffff"
    rounded: "{rounded.md}"
    padding: "0.7rem 1rem"
  button-primary-hover:
    backgroundColor: "#9e188c"
    textColor: "#ffffff"
    rounded: "{rounded.md}"
    padding: "0.7rem 1rem"
  button-secondary:
    backgroundColor: "{colors.border}"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    padding: "0.7rem 1rem"
  button-danger:
    backgroundColor: "{colors.danger-red}"
    textColor: "#ffffff"
    rounded: "{rounded.full}"
    padding: "0.75rem 2.1rem"
  button-pill:
    backgroundColor: "rgba(255, 255, 255, 0.14)"
    textColor: "#ffffff"
    rounded: "{rounded.md}"
    padding: "0.3rem 0.65rem"
  input-field:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
    padding: "0.55rem 0.7rem"
    border: "1px solid {colors.border}"
  bingo-cell:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
    border: "1px solid {colors.border}"
---

# Design System: Melbete Bingo

## 1. Overview

**Creative North Star: "The Community Board"**

Melbete Bingo's visual system evokes a welcoming neighbourhood game hall — familiar, tactile, and trustworthy. The palette pairs deep deep plums with a dependable blue and warm highlight cyan accents, creating an interface that feels both celebratory and financially responsible. Every screen is built for mobile thumbs on Telegram, with bold hit areas, clear hierarchy, and zero visual clutter.

The system explicitly rejects the tropes of shady betting sites: no flashing win-banners, no countdown pressure gimmicks, no glassmorphism, no gradient text. When money is on the line, clarity is the differentiator.

**Key Characteristics:**
- Warm green + blue foundation, gold accent — culturally resonant, not generic
- Large, thumb-friendly tap targets with tactile press states
- Flat layouts with soft lift on interactive elements
- Minimal chrome — the game board and balances own the visual weight
- Legible at a glance: every number, label, and action speaks clearly

## 2. Colors

The palette is anchored by deep plum (the header and brand anchor), signal magenta (primary actions), and highlight cyan (celebration and marking). Neutrals stay cool-leaning to keep the interface crisp against the warm greens.

### Primary
- **Brand Plum** (#382d4c — `oklch(30% 0.05 300)`): The brand anchor. Used as the top bar and admin header gradient base. Represents trust and the Ethiopian identity.
- **Brand Plum Light** (#554275 — `oklch(40% 0.06 300)`): The lighter end of the header gradient. Paired with the deep green for a refined two-tone bar.

### Accent
- **Signal Magenta** (#b527a1 — `oklch(55% 0.18 335)`): The primary action colour. Used for primary buttons, links, and selected method states. It signals "tap here to proceed with confidence."
- **Highlight Cyan** (#2bcbd3 — `oklch(72% 0.15 200)`): Warning and celebration. Used for countdown banners, the BINGO claim button's marked cells, and status indicators. Never used for destructive actions.
- **Highlight Cyan Bright** (#01e5ee — `oklch(80% 0.14 200)`): The colour of marked Bingo numbers on the card. Brighter than the warning gold to pop against the white card surface.

### Secondary
- **Secondary Purple** (#6b43f4 — `oklch(50% 0.22 285)`): Used sparingly for the current called number display and free-space markers. Adds a touch of vibrance without competing with the green + blue + gold trio.

### Feedback
- **Success Green** (#10b981 — `oklch(65% 0.18 160)`): Deposits, positive balances, confirmations, approved states.
- **Danger Red** (#ef4444 — `oklch(55% 0.22 30)`): The BINGO claim button, withdrawals, errors, rejected states, toast errors.

### Neutral
- **Neutral Background** (#f6f7fb — `oklch(95% 0.005 260)`): The page background. Cool-tinted off-white, never warm.
- **Surface** (#ffffff): Card backgrounds, modal content, input fields. Pure white for maximum legibility.
- **Ink** (#2b2538 — `oklch(25% 0.02 300)`): Body text. High-contrast against the light background.
- **Border** (#e8e6ef — `oklch(88% 0.01 260)`): Subtle separation lines and input strokes.
- **Muted** (#766e88 — `oklch(55% 0.02 300)`): Secondary text, placeholders, timestamps.

### Named Rules

**The One Voice Rule.** The green header + blue actions + gold accent trio should carry 90% of the visual weight. Purple and red are reserved for their specific roles. If a screen uses more than the trio plus one feedback colour, it's too busy.

**The Colour-Bind Safety Rule.** Never convey information by hue alone. Marked/unmarked Bingo cells use both colour (gold) and a distinct icon (✓); status indicators pair colour with text labels. The palette is colour-blind safe with all critical pairs distinguishable by lightness and saturation.

## 3. Typography

**Body Font:** System stack (`-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif`)

Same family throughout — one sans-serif stack for everything.

**Character:** Clean, familiar, and highly legible on small screens. No custom fonts to load in a Telegram Mini App where every kilobyte counts. Weight is the primary differentiator: 800 for display numbers and game state, 700 for labels and buttons, 400 for body copy.

### Hierarchy
- **Display** (800, `clamp(2rem, 6vw, 3.5rem)`, 1.15): The current called number. Only appears once per game view.
- **Headline** (700, `clamp(1.1rem, 4vw, 1.5rem)`, 1.2): Section titles, modal headings, lobby heading.
- **Title** (700, `0.85rem`, 1.3): Button labels, pill text, BINGO card header letters (B-I-N-G-O).
- **Body** (400, `0.85rem`, 1.5): All primary content text. Capped at 65–75ch where it appears in paragraphs.
- **Label** (700, `0.78rem`, 1.15, 0.05em letter-spacing, uppercase): Tiny uppercase labels ("BALANCE", deposit/withdraw field labels).
- **Small/Muted** (400, `0.78rem`, 1.4): Secondary information, timestamps, helper text.

### Named Rules

**The No-Custom-Font Rule.** Melbete Bingo loads inside Telegram's Mini App WebView. Every additional font file is a 200–400ms penalty on cold load. The system stack is fast, familiar, and renders perfectly on every device Ethiopean users actually use. No exceptions.

## 4. Elevation

Depth is conveyed through soft shadows on interactive surfaces, not tonal layering. The background is flat; cards and panels are subtly lifted.

### Shadow Vocabulary
- **Card / Modal Content** (`box-shadow: 0 4px 20px rgba(0, 0, 0, 0.06)`): Panels, modal content sheets, admin cards. A gentle lift that says "this is a surface you interact with."
- **Toast** (no explicit shadow; sits at z-index 200 on a dark background): The only true overlay element. Its high contrast background (#2b2538) provides separation without an additional shadow.

Surfaces at rest (the page background, the header) are completely flat. Shadows are reserved for interactive or elevated containers.

### Named Rules

**The Lifted-By-Default Rule.** Cards and modals sit above the background with a soft shadow. Buttons are flat until hovered. No element should have both a border and a wide shadow — pick one.

## 5. Components

### Buttons
- **Shape:** Gently curved (0.6rem radius).
- **Primary (Signal Magenta):** `background: #b527a1`, white text, `padding: 0.7rem 1rem`, full width on mobile. Hover shifts to `#1d4ed8`. No shadow.
- **Secondary (Border/Neutral):** `background: #e8e6ef`, ink text. For non-primary actions.
- **Danger (BINGO Claim):** `background: #ef4444`, white text, full pill shape (999px radius), `padding: 0.75rem 2.1rem`, bold 800 weight. The biggest button on screen — intentionally dramatic.
- **Pill (Top Bar):** Semi-transparent white (`rgba(255, 255, 255, 0.14)`), white text, 0.6rem radius. For header actions like deposit/withdraw.
- **Toggle:** Border-only style with a dot indicator. Green dot = on, red dot = off. The toggle border turns green when active.
- **Text Button:** No background, `color: var(--primary-color)`, bold. For low-emphasis actions like "Change" cartela.

All buttons have `cursor: pointer` and transition background 0.15s ease.

### Chips / Pills
- **Called Numbers:** `background: #e8e6ef` (border colour), `border-radius: 999px`, `padding: 0.2rem 0.55rem`, `font-size: 0.72rem`, bold 700. Tightly packed with 0.3rem gap.
- **Balance Pill:** Semi-transparent white background inside the header. Bonus variant uses amber-tinted background (`rgba(43, 203, 211, 0.28)`, text `#fde68a`).
- **Pool/Prize Pill:** Card-style (`background: #ffffff`, `border: 1px solid #e8e6ef`, `border-radius: 999px`, `padding: 0.4rem 0.8rem`). For displaying prize pool and game pattern info.

### Cards
- **Bingo Card:** A 5×5 grid with 0.3rem gap. Cells are `aspect-ratio: 1`, `border-radius: 0.4rem`, `border: 1px solid #e8e6ef`. The card header row uses colour-coded cells (B-blue, I-red, N-purple, G-green, O-amber).
  - Free space: purple background, white text.
  - Marked cell: highlight cyan background (`#fbbf24`), dark text (`#422006`).
- **Cartela (Lobby) Grid:** 8-column responsive grid (10-column on wider screens). Cells are square, bordered, with `variant: "primary"` for taken and `"success"` for the player's selection.
- **Modal Content:** Full-width bottom sheet (`border-radius: 1rem 1rem 0 0`, `max-width: 480px`). Soft shadow, white background, scrollable up to 88vh.

### Inputs / Fields
- **Style:** `border: 1px solid #e8e6ef`, `border-radius: 0.5rem`, `padding: 0.55rem 0.7rem`, `font-size: 1rem`, full width.
- **Focus:** Outer border transitions to `var(--primary-color)` (not currently implemented — a future enhancement).
- **Labels:** Always visible (not placeholder-based). Small uppercase label above the field.
- **File Input:** For deposit screenshots, with a preview image below.
- **Error / Disabled:** Error uses danger red text; disabled uses 0.6 opacity.

### Navigation
- **Top Bar:** Fixed/sticky header with green gradient background (`linear-gradient(135deg, #382d4c, #554275)`). Contains balances on the left, actions on the right. Flex-wrap enabled for narrow screens.
- **Admin Tabs:** Horizontal scrollable tab bar with pill-shaped tab buttons. Active tab gets the signal magenta background, inactive tabs use the page background.

## 6. Do's and Don'ts

### Do:
- **Do** use the green header + blue primary + gold accent trio as the dominant visual system.
- **Do** make tap targets at least 44×44px on mobile.
- **Do** use system fonts only — no custom font loading.
- **Do** use soft shadows on cards and modals for lift.
- **Do** use `text-wrap: balance` on headings and `text-wrap: pretty` on body text.
- **Do** keep the called number display large (2.6rem, 800 weight) — it's the focal point of the game view.
- **Do** use full-width buttons on mobile for primary actions.
- **Do** cap modal widths at 480px and use bottom-sheet entry on mobile.
- **Do** mark Bingo cells with both colour (gold) and context for colour-blind safety.

### Don't:
- **Don't** use flashing "win big now!" banners, countdown pressure tricks, or scam-betting visual language.
- **Don't** use glassmorphism, gradient text, or decorative grid backgrounds.
- **Don't** use border-left/right as coloured accent stripes on any element.
- **Don't** use side-stripe borders, repeating-linear-gradient stripe backgrounds, or hand-drawn sketchy SVG illustrations.
- **Don't** use the hero-metric template (big number, small label, supporting stats).
- **Don't** add numbered section markers (01 / 02 / 03) as decoration.
- **Don't** pair a 1px border with a wide box-shadow (M ≥ 16px blur) on the same element — pick one.
- **Don't** use border-radius larger than 12–16px on cards or sections; full pill is reserved for buttons and chips only.
- **Don't** pair similar sans-serif fonts — use one system stack throughout.
- **Don't** use tiny uppercase tracked "eyebrow" text above every section.
- **Don't** use identical card grids repeated endlessly; vary layout and content.
- **Don't** convey state by colour alone — always pair with text, icon, or pattern.
