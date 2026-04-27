---
version: alpha
name: Weather Stratus
description: Forecast app: stratus grey, rain blue, storm amber.
colors:
  primary: "#0F1620"
  secondary: "#61738C"
  tertiary: "#F2A03D"
  neutral: "#EDF1F7"
  surface: "#FFFFFF"
  on-primary: "#FFFFFF"
typography:
  display:
    fontFamily: Outfit
    fontSize: 4rem
    fontWeight: 300
    letterSpacing: "-0.04em"
  h1:
    fontFamily: Outfit
    fontSize: 2.1rem
    fontWeight: 400
  body:
    fontFamily: Outfit
    fontSize: 0.95rem
    lineHeight: 1.55
  label:
    fontFamily: Outfit
    fontSize: 0.72rem
    fontWeight: 500
    letterSpacing: "0.14em"
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

A weather-forecast palette: stratus greys, rain-blue primary, storm-amber alert accent.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#0F1620`):** Headlines and core text.
- **Secondary (`#61738C`):** Borders, captions, and metadata.
- **Tertiary (`#F2A03D`):** The sole driver for interaction. Reserve it.
- **Neutral (`#EDF1F7`):** The page foundation.

## Typography

- **display:** Outfit 4rem
- **h1:** Outfit 2.1rem
- **body:** Outfit 0.95rem
- **label:** Outfit 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
