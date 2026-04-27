---
version: alpha
name: Ceramics Kiln
description: Wood-fired ceramics: clay beige, ash grey, iron glaze.
colors:
  primary: "#1E1711"
  secondary: "#7E7466"
  tertiary: "#A8442A"
  neutral: "#EADDC8"
  surface: "#F3E7D1"
  on-primary: "#F3E7D1"
typography:
  display:
    fontFamily: Caveat
    fontSize: 5rem
    fontWeight: 700
    letterSpacing: "-0.01em"
  h1:
    fontFamily: Cormorant Garamond
    fontSize: 2.4rem
    fontWeight: 600
  body:
    fontFamily: Lora
    fontSize: 1rem
    lineHeight: 1.7
  label:
    fontFamily: Lora
    fontSize: 0.74rem
    letterSpacing: "0.12em"
rounded:
  sm: 6px
  md: 12px
  lg: 22px
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

A ceramics-studio palette: clay beige paper, iron-glaze accent, hand-lettered feel.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#1E1711`):** Headlines and core text.
- **Secondary (`#7E7466`):** Borders, captions, and metadata.
- **Tertiary (`#A8442A`):** The sole driver for interaction. Reserve it.
- **Neutral (`#EADDC8`):** The page foundation.

## Typography

- **display:** Caveat 5rem
- **h1:** Cormorant Garamond 2.4rem
- **body:** Lora 1rem
- **label:** Lora 0.74rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
