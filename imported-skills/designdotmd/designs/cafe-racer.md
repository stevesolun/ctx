---
version: alpha
name: Cafe Racer
description: Cafe-racer: engine black, tank stripe, chrome spoke.
colors:
  primary: "#E8E4DB"
  secondary: "#9B968C"
  tertiary: "#C42E2E"
  neutral: "#0B0B0C"
  surface: "#141415"
  on-primary: "#E8E4DB"
typography:
  display:
    fontFamily: Oswald
    fontSize: 5rem
    fontWeight: 700
    letterSpacing: "0.02em"
  h1:
    fontFamily: Oswald
    fontSize: 2.4rem
    fontWeight: 600
  body:
    fontFamily: Inter
    fontSize: 0.95rem
    lineHeight: 1.55
  label:
    fontFamily: Oswald
    fontSize: 0.82rem
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

A motorcycle-brand palette: engine-black surface, tank-stripe cream, chrome-silver highlights.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#E8E4DB`):** Headlines and core text.
- **Secondary (`#9B968C`):** Borders, captions, and metadata.
- **Tertiary (`#C42E2E`):** The sole driver for interaction. Reserve it.
- **Neutral (`#0B0B0C`):** The page foundation.

## Typography

- **display:** Oswald 5rem
- **h1:** Oswald 2.4rem
- **body:** Inter 0.95rem
- **label:** Oswald 0.82rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
