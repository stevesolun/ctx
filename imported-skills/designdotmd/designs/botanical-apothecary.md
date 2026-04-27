---
version: alpha
name: Botanical Apothecary
description: Herb apothecary: amber bottle, linen label, eucalyptus.
colors:
  primary: "#22201B"
  secondary: "#7A7264"
  tertiary: "#5A7452"
  neutral: "#F1EADD"
  surface: "#F9F3E5"
  on-primary: "#F9F3E5"
typography:
  display:
    fontFamily: Cormorant Garamond
    fontSize: 4.5rem
    fontWeight: 500
    letterSpacing: "-0.015em"
  h1:
    fontFamily: Cormorant Garamond
    fontSize: 2.4rem
    fontWeight: 500
  body:
    fontFamily: Lora
    fontSize: 1rem
    lineHeight: 1.7
  label:
    fontFamily: Lora
    fontSize: 0.74rem
    fontWeight: 600
    letterSpacing: "0.16em"
rounded:
  sm: 4px
  md: 8px
  lg: 14px
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

An apothecary palette: amber glass, linen labels, eucalyptus green highlights.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#22201B`):** Headlines and core text.
- **Secondary (`#7A7264`):** Borders, captions, and metadata.
- **Tertiary (`#5A7452`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F1EADD`):** The page foundation.

## Typography

- **display:** Cormorant Garamond 4.5rem
- **h1:** Cormorant Garamond 2.4rem
- **body:** Lora 1rem
- **label:** Lora 0.74rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
