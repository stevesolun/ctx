---
version: alpha
name: Botanical
description: Pressed leaves, tea-stained paper, a copper ink.
colors:
  primary: "#2A3A1F"
  secondary: "#7D8064"
  tertiary: "#B56A3F"
  neutral: "#F0EAD6"
  surface: "#F7F1DC"
  on-primary: "#F7F1DC"
typography:
  display:
    fontFamily: Cormorant Garamond
    fontSize: 4.75rem
    fontWeight: 500
    letterSpacing: "-0.01em"
  h1:
    fontFamily: Cormorant Garamond
    fontSize: 2.75rem
    fontWeight: 500
  body:
    fontFamily: Lora
    fontSize: 1.05rem
    lineHeight: 1.7
  label:
    fontFamily: Inter
    fontSize: 0.72rem
    letterSpacing: "0.08em"
rounded:
  sm: 2px
  md: 6px
  lg: 12px
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

A palette for slow publications and plant-adjacent brands. Deep herb green primary, parchment surface, copper accent.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#2A3A1F`):** Headlines and core text.
- **Secondary (`#7D8064`):** Borders, captions, and metadata.
- **Tertiary (`#B56A3F`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F0EAD6`):** The page foundation.

## Typography

- **display:** Cormorant Garamond 4.75rem
- **h1:** Cormorant Garamond 2.75rem
- **body:** Lora 1.05rem
- **label:** Inter 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
