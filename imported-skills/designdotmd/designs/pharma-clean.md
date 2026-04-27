---
version: alpha
name: Pharma Clean
description: Hospital-white, FDA-blue, sterile hairlines.
colors:
  primary: "#0B1E3A"
  secondary: "#5E6F88"
  tertiary: "#1976D2"
  neutral: "#F5F8FC"
  surface: "#FFFFFF"
  on-primary: "#FFFFFF"
typography:
  display:
    fontFamily: Inter
    fontSize: 3.5rem
    fontWeight: 500
    letterSpacing: "-0.02em"
  h1:
    fontFamily: Inter
    fontSize: 1.9rem
    fontWeight: 500
  body:
    fontFamily: Inter
    fontSize: 0.95rem
    lineHeight: 1.6
  label:
    fontFamily: Inter
    fontSize: 0.72rem
    fontWeight: 600
    letterSpacing: "0.08em"
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

A pharmaceutical-product palette: paper white, medical blue, strict hairline rules.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#0B1E3A`):** Headlines and core text.
- **Secondary (`#5E6F88`):** Borders, captions, and metadata.
- **Tertiary (`#1976D2`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F5F8FC`):** The page foundation.

## Typography

- **display:** Inter 3.5rem
- **h1:** Inter 1.9rem
- **body:** Inter 0.95rem
- **label:** Inter 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
