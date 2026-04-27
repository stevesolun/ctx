---
version: alpha
name: Record Sleeve
description: 1970s gatefold: ochre, umber, liner notes in serif.
colors:
  primary: "#2A1B10"
  secondary: "#7A6648"
  tertiary: "#C77A2B"
  neutral: "#F4E9D1"
  surface: "#FBF2DE"
  on-primary: "#FBF2DE"
typography:
  display:
    fontFamily: Abril Fatface
    fontSize: 5rem
    fontWeight: 400
  h1:
    fontFamily: Abril Fatface
    fontSize: 2.6rem
    fontWeight: 400
  body:
    fontFamily: Libre Caslon Text
    fontSize: 1rem
    lineHeight: 1.7
  label:
    fontFamily: Bebas Neue
    fontSize: 0.85rem
    letterSpacing: "0.16em"
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

A record-store system drenched in dust and sun.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#2A1B10`):** Headlines and core text.
- **Secondary (`#7A6648`):** Borders, captions, and metadata.
- **Tertiary (`#C77A2B`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F4E9D1`):** The page foundation.

## Typography

- **display:** Abril Fatface 5rem
- **h1:** Abril Fatface 2.6rem
- **body:** Libre Caslon Text 1rem
- **label:** Bebas Neue 0.85rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
