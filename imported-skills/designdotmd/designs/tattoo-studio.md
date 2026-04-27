---
version: alpha
name: Tattoo Studio
description: Tattoo parlor: ink black, stencil blue, flash-sheet red.
colors:
  primary: "#EEEAE0"
  secondary: "#7D7A72"
  tertiary: "#DC143C"
  neutral: "#0A0A0A"
  surface: "#141414"
  on-primary: "#0A0A0A"
typography:
  display:
    fontFamily: Bebas Neue
    fontSize: 5.5rem
    fontWeight: 400
    letterSpacing: "0.02em"
  h1:
    fontFamily: Bebas Neue
    fontSize: 2.8rem
    fontWeight: 400
  body:
    fontFamily: Inter
    fontSize: 0.92rem
    lineHeight: 1.5
  label:
    fontFamily: Bebas Neue
    fontSize: 0.88rem
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

A tattoo-studio palette: ink black, stencil blue, flash-sheet red for traditional motifs.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#EEEAE0`):** Headlines and core text.
- **Secondary (`#7D7A72`):** Borders, captions, and metadata.
- **Tertiary (`#DC143C`):** The sole driver for interaction. Reserve it.
- **Neutral (`#0A0A0A`):** The page foundation.

## Typography

- **display:** Bebas Neue 5.5rem
- **h1:** Bebas Neue 2.8rem
- **body:** Inter 0.92rem
- **label:** Bebas Neue 0.88rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
