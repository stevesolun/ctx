---
version: alpha
name: EV Silver
description: Electric-vehicle showroom: liquid silver, voltage blue.
colors:
  primary: "#0A0F16"
  secondary: "#6C7788"
  tertiary: "#0096FF"
  neutral: "#EEF1F4"
  surface: "#FFFFFF"
  on-primary: "#FFFFFF"
typography:
  display:
    fontFamily: Outfit
    fontSize: 4.5rem
    fontWeight: 300
    letterSpacing: "-0.04em"
  h1:
    fontFamily: Outfit
    fontSize: 2.4rem
    fontWeight: 300
    letterSpacing: "-0.03em"
  body:
    fontFamily: Outfit
    fontSize: 0.95rem
    lineHeight: 1.55
  label:
    fontFamily: Outfit
    fontSize: 0.72rem
    fontWeight: 500
    letterSpacing: "0.18em"
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

A future-automotive system: cool-silver chrome, electric-blue accent.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#0A0F16`):** Headlines and core text.
- **Secondary (`#6C7788`):** Borders, captions, and metadata.
- **Tertiary (`#0096FF`):** The sole driver for interaction. Reserve it.
- **Neutral (`#EEF1F4`):** The page foundation.

## Typography

- **display:** Outfit 4.5rem
- **h1:** Outfit 2.4rem
- **body:** Outfit 0.95rem
- **label:** Outfit 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
