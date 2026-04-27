---
version: alpha
name: Gallery White
description: Contemporary gallery: nothing on the walls but art.
colors:
  primary: "#1C1C1B"
  secondary: "#8F8F8D"
  tertiary: "#595957"
  neutral: "#F9F9F7"
  surface: "#FFFFFF"
  on-primary: "#FFFFFF"
typography:
  display:
    fontFamily: Cormorant Garamond
    fontSize: 5.5rem
    fontWeight: 300
    letterSpacing: "-0.02em"
  h1:
    fontFamily: Cormorant Garamond
    fontSize: 3rem
    fontWeight: 300
  body:
    fontFamily: Inter
    fontSize: 0.95rem
    lineHeight: 1.65
  label:
    fontFamily: Inter
    fontSize: 0.7rem
    letterSpacing: "0.14em"
rounded:
  sm: 0px
  md: 0px
  lg: 0px
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

Maximum whitespace. Near-white surface, thin rules, a single charcoal for typography. Built for portfolios.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#1C1C1B`):** Headlines and core text.
- **Secondary (`#8F8F8D`):** Borders, captions, and metadata.
- **Tertiary (`#595957`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F9F9F7`):** The page foundation.

## Typography

- **display:** Cormorant Garamond 5.5rem
- **h1:** Cormorant Garamond 3rem
- **body:** Inter 0.95rem
- **label:** Inter 0.7rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
