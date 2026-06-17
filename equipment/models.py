from django.db import models

class ProcessGroup(models.Model):
    name       = models.CharField(max_length=100, unique=True)
    efficiency = models.FloatField(default=1.0)
    eq_count   = models.IntegerField(default=0)
    def __str__(self): return self.name

class Product(models.Model):
    name   = models.CharField(max_length=100, unique=True)
    active = models.BooleanField(default=True)
    def __str__(self): return self.name

class UPH(models.Model):
    process_group = models.ForeignKey(ProcessGroup, on_delete=models.CASCADE)
    lot_cd        = models.CharField(max_length=50)
    process_nm    = models.CharField(max_length=100)
    apw           = models.FloatField(default=0)
    class Meta:
        unique_together = ('lot_cd', 'process_group', 'process_nm')

class ProductTAT(models.Model):
    product        = models.ForeignKey(Product, on_delete=models.CASCADE)
    days_to_fabout = models.FloatField(default=0)

class ProductionPlan(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    year    = models.IntegerField()
    month   = models.IntegerField()
    qty     = models.IntegerField(default=0)
    class Meta:
        unique_together = ('product', 'year', 'month')

class EquipmentSchedule(models.Model):
    process_group = models.ForeignKey(ProcessGroup, on_delete=models.CASCADE)
    eq_id         = models.CharField(max_length=100)
    arrive_date   = models.CharField(max_length=20)
    apply_date    = models.CharField(max_length=20)
    note          = models.TextField(blank=True)

class ProcessTAT(models.Model):
    process_group  = models.ForeignKey(ProcessGroup, on_delete=models.CASCADE)
    lot_cd         = models.CharField(max_length=50)
    process_nm     = models.CharField(max_length=100)
    days_to_fabout = models.FloatField(default=0)
    class Meta:
        unique_together = ('lot_cd', 'process_nm')
