---
version: alpha
name: Candy Shop
description: Bubble gum, cherry red, sugar rush.
colors:
  primary: "#3C0D28"
  secondary: "#B2578C"
  tertiary: "#FF2D6E"
  neutral: "#FFE9F2"
  surface: "#FFFFFF"
  on-primary: "#FFFFFF"
typography:
  display:
    fontFamily: Fraunces
    fontSize: 4.5rem
    fontWeight: 700
    letterSpacing: "-0.03em"
  h1:
    fontFamily: Fraunces
    fontSize: 2.5rem
    fontWeight: 700
  body:
    fontFamily: Nunito
    fontSize: 1rem
    lineHeight: 1.6
  label:
    fontFamily: Nunito
    fontSize: 0.72rem
    letterSpacing: "0.04em"
rounded:
  sm: 14px
  md: 24px
  lg: 40px
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

Full-volume joy. Bubblegum pink, cherry red, rounded everything. Not for serious enterprise.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#3C0D28`):** Headlines and core text.
- **Secondary (`#B2578C`):** Borders, captions, and metadata.
- **Tertiary (`#FF2D6E`):** The sole driver for interaction. Reserve it.
- **Neutral (`#FFE9F2`):** The page foundation.

## Typography

- **display:** Fraunces 4.5rem
- **h1:** Fraunces 2.5rem
- **body:** Nunito 1rem
- **label:** Nunito 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
