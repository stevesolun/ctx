---
version: alpha
name: Campus Collegiate
description: Ivy-league collegiate: maroon, oatmeal, crest gold.
colors:
  primary: "#2E0D12"
  secondary: "#8A6E73"
  tertiary: "#B8842C"
  neutral: "#F1EADF"
  surface: "#FBF4E7"
  on-primary: "#FBF4E7"
typography:
  display:
    fontFamily: Merriweather
    fontSize: 4.25rem
    fontWeight: 700
    letterSpacing: "-0.01em"
  h1:
    fontFamily: Merriweather
    fontSize: 2.3rem
    fontWeight: 700
  body:
    fontFamily: Merriweather
    fontSize: 1rem
    lineHeight: 1.7
  label:
    fontFamily: Inter
    fontSize: 0.72rem
    fontWeight: 700
    letterSpacing: "0.16em"
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

A collegiate brand palette: deep maroon, oatmeal surfaces, crest gold accent.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#2E0D12`):** Headlines and core text.
- **Secondary (`#8A6E73`):** Borders, captions, and metadata.
- **Tertiary (`#B8842C`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F1EADF`):** The page foundation.

## Typography

- **display:** Merriweather 4.25rem
- **h1:** Merriweather 2.3rem
- **body:** Merriweather 1rem
- **label:** Inter 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
