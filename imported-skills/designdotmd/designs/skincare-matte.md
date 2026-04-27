---
version: alpha
name: Skincare Matte
description: Matte bottle minimal: oat, stone grey, graphite label.
colors:
  primary: "#1B1917"
  secondary: "#8C847A"
  tertiary: "#3E3832"
  neutral: "#ECE6D9"
  surface: "#F6F1E4"
  on-primary: "#F6F1E4"
typography:
  display:
    fontFamily: Jost
    fontSize: 4rem
    fontWeight: 300
    letterSpacing: "-0.02em"
  h1:
    fontFamily: Jost
    fontSize: 2.2rem
    fontWeight: 300
  body:
    fontFamily: Jost
    fontSize: 0.98rem
    lineHeight: 1.65
  label:
    fontFamily: Jost
    fontSize: 0.72rem
    fontWeight: 400
    letterSpacing: "0.24em"
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

A modern skincare palette: oat-cream surface, graphite text, stone-grey minor hairlines.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#1B1917`):** Headlines and core text.
- **Secondary (`#8C847A`):** Borders, captions, and metadata.
- **Tertiary (`#3E3832`):** The sole driver for interaction. Reserve it.
- **Neutral (`#ECE6D9`):** The page foundation.

## Typography

- **display:** Jost 4rem
- **h1:** Jost 2.2rem
- **body:** Jost 0.98rem
- **label:** Jost 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
