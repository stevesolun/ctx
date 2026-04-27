---
version: alpha
name: Old Money
description: Hunter green, gold leaf, embossed cream.
colors:
  primary: "#1F3A2C"
  secondary: "#7B8271"
  tertiary: "#B89155"
  neutral: "#F0EADB"
  surface: "#F8F1DF"
  on-primary: "#F8F1DF"
typography:
  display:
    fontFamily: Cormorant
    fontSize: 5rem
    fontWeight: 500
    letterSpacing: "-0.015em"
  h1:
    fontFamily: Cormorant
    fontSize: 2.75rem
    fontWeight: 500
  body:
    fontFamily: EB Garamond
    fontSize: 1.1rem
    lineHeight: 1.7
  label:
    fontFamily: Inter
    fontSize: 0.7rem
    letterSpacing: "0.18em"
rounded:
  sm: 0px
  md: 2px
  lg: 4px
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

Quiet-luxury palette. Hunter green primary, gold accent, cream surface. Restrained but unmistakably premium.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#1F3A2C`):** Headlines and core text.
- **Secondary (`#7B8271`):** Borders, captions, and metadata.
- **Tertiary (`#B89155`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F0EADB`):** The page foundation.

## Typography

- **display:** Cormorant 5rem
- **h1:** Cormorant 2.75rem
- **body:** EB Garamond 1.1rem
- **label:** Inter 0.7rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
