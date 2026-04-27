---
version: alpha
name: Tarot Deck
description: Rider-Waite vibes: midnight blue, gilt edges, mystic sigils.
colors:
  primary: "#F3EAD0"
  secondary: "#A89775"
  tertiary: "#E5B85C"
  neutral: "#0C1433"
  surface: "#15204A"
  on-primary: "#F3EAD0"
typography:
  display:
    fontFamily: Cormorant Garamond
    fontSize: 5rem
    fontWeight: 500
    letterSpacing: "-0.01em"
  h1:
    fontFamily: Cormorant Garamond
    fontSize: 2.5rem
    fontWeight: 500
  body:
    fontFamily: EB Garamond
    fontSize: 1.05rem
    lineHeight: 1.7
  label:
    fontFamily: IM Fell English
    fontSize: 0.82rem
    letterSpacing: "0.12em"
rounded:
  sm: 2px
  md: 4px
  lg: 8px
spacing:
  sm: 8px
  md: 16px
  lg: 32px
components:
  button-primary:
    backgroundColor: "{colors.tertiary}"
    textColor: "{colors.on-primary}"
    rounded: "{rounded.md}"
    padding: 12px 20px
  card:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.primary}"
    rounded: "{rounded.lg}"
    padding: 24px
---
## Overview

A mystical palette for astrology/tarot apps: midnight blue, gilt accent, occult serif.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#F3EAD0`):** Headlines and core text.
- **Secondary (`#A89775`):** Borders, captions, and metadata.
- **Tertiary (`#E5B85C`):** The sole driver for interaction. Reserve it.
- **Neutral (`#0C1433`):** The page foundation.

## Typography

- **display:** Cormorant Garamond 5rem
- **h1:** Cormorant Garamond 2.5rem
- **body:** EB Garamond 1.05rem
- **label:** IM Fell English 0.82rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
