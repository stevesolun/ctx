---
version: alpha
name: Museum Plaque
description: Museum-wall white: didone, hairline rules, placard caps.
colors:
  primary: "#14130F"
  secondary: "#72706A"
  tertiary: "#3F3A33"
  neutral: "#F4F0E6"
  surface: "#FBF8EF"
  on-primary: "#FBF8EF"
typography:
  display:
    fontFamily: Bodoni Moda
    fontSize: 6rem
    fontWeight: 400
    letterSpacing: "-0.02em"
  h1:
    fontFamily: Bodoni Moda
    fontSize: 2.8rem
    fontWeight: 400
  body:
    fontFamily: Source Serif 4
    fontSize: 1rem
    lineHeight: 1.7
  label:
    fontFamily: Inter
    fontSize: 0.68rem
    fontWeight: 500
    letterSpacing: "0.3em"
rounded:
  sm: 0px
  md: 0px
  lg: 0px
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

A museum catalog system: didone display, uppercase placards, strict rag-right body.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#14130F`):** Headlines and core text.
- **Secondary (`#72706A`):** Borders, captions, and metadata.
- **Tertiary (`#3F3A33`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F4F0E6`):** The page foundation.

## Typography

- **display:** Bodoni Moda 6rem
- **h1:** Bodoni Moda 2.8rem
- **body:** Source Serif 4 1rem
- **label:** Inter 0.68rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
